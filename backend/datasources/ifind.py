"""同花顺 iFinD 数据源适配器（QuantAPI / HTTP 量化接口）

为什么走 QuantAPI 而不是客户端：
    iFinD Mac 客户端只开 CEF 调试端口（127.0.0.1:9222），**不提供本地数据 HTTP 服务**，
    也没有内置 Python SDK。后端进程能直接调的官方通道只有 QuantAPI（quantapi.51ifind.com），
    它靠 refresh_token 换 access_token，与客户端是否开着无关。

前置条件（任一不满足则本数据源自动禁用，不影响其他源）：
    1. iFinD 账号已开通「量化接口 / QuantAPI」权限；
    2. 在 iFinD 官网「量化接口 -> 我的接口 -> 刷新令牌」拿到 refresh_token；
    3. 设置环境变量 IFIND_REFRESH_TOKEN=xxxx（或在 config.py 里改 IFIND_REFRESH_TOKEN 默认值）。

原始单位：
    volume = 股（iFinD A股成交量单位就是股）
    amount = 元
    与 standards.SOURCE_UNITS["ifind"] 保持一致。

自检：
    cd backend && python -m datasources.ifind --probe
"""
from __future__ import annotations

import json
import logging
import os
import ssl
import urllib.error
import urllib.request
from datetime import datetime
from typing import Dict, List, Optional

from . import standards as std
from .base import BaseDataSource, DataSourceError

logger = logging.getLogger(__name__)

_API_BASE = os.getenv("IFIND_API_BASE", "https://quantapi.51ifind.com/api/v1")


def _read_env_file(key: str) -> str:
    """从 quant-system/.env 读单个键（本项目没有全局 dotenv 加载器，故在此自兜底）。

    只读取指定 key，不污染 os.environ、不影响其他配置的既有读取逻辑。
    """
    env_path = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(__file__))), ".env")
    try:
        with open(env_path, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                k, _, v = line.partition("=")
                if k.strip() == key:
                    return v.strip().strip('"').strip("'")
    except OSError:
        pass
    return ""


_REFRESH_TOKEN = os.getenv("IFIND_REFRESH_TOKEN", "").strip() or _read_env_file("IFIND_REFRESH_TOKEN")
_TIMEOUT = int(os.getenv("IFIND_TIMEOUT", "20"))

# URL 里带中文/brackets 时的编码问题在 urllib 里不存在，保持默认 SSL 上下文
_CTX = ssl.create_default_context()

# quant 的 period -> iFinD Interval
_INTERVAL = {"daily": "D", "weekly": "W", "monthly": "M", "1min": "1", "5min": "5",
             "15min": "15", "30min": "30", "60min": "60"}
# quant 的 adjust -> iFinD Adjustment
_ADJUSTMENT = {"qfq": "ForwardAdjust", "hfq": "BackwardAdjust", "none": "NoAdjust", "": "NoAdjust"}

_KLINE_INDICATORS = "open,high,low,close,volume,amount,changeRatio"
# 实时行情指标：只用确定存在的核心指标（任一非法指标会让整个请求报错），
# PE/PB 等扩展指标按需在此追加，并在 _QUOTE_FALLBACK 之外单独容错。
_QUOTE_INDICATORS = ("open,high,low,latest,preClose,volume,amount,"
                     "change,changeRatio,turnoverRatio")
_QUOTE_FALLBACK = "latest,preClose,close,volume,amount"

# iFinD 指标名 -> 标准字段名（standardizer 的别名表里没有这几个驼峰名，必须在此显式映射，
# 否则 "latest" 不会被识别为 price，整条记录会因必填缺失被丢掉）
_FIELD_MAP = {
    "latest": "price",
    "changeRatio": "pct_change",
    "turnoverRatio": "turnover",
    "preClose": "pre_close",
}


class IfindSource(BaseDataSource):
    source_name = "ifind"
    capabilities = {"kline", "quote"}
    raw_units = {"volume": "股", "amount": "元"}

    def __init__(self, refresh_token: str | None = None):
        self._refresh_token = (refresh_token or _REFRESH_TOKEN or "").strip()
        self._access_token: Optional[str] = None
        self._token_at: float = 0.0
        if not self._refresh_token:
            logger.info("[ifind] 未配置 IFIND_REFRESH_TOKEN，该源自动禁用")

    # ------------------------------------------------------------------
    # 可用性
    # ------------------------------------------------------------------
    @property
    def enabled(self) -> bool:
        return bool(self._refresh_token)

    def supports(self, capability: str) -> bool:
        """未配置令牌时对任何能力都说「不支持」，路由层直接跳过，不产生重试开销。"""
        return self.enabled and capability in self.capabilities

    # ------------------------------------------------------------------
    # HTTP 基础
    # ------------------------------------------------------------------
    def _post(self, path: str, payload: dict, token: str) -> dict:
        url = f"{_API_BASE}/{path}"
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        req = urllib.request.Request(
            url, data=body, method="POST",
            headers={"Content-Type": "application/json", "access_token": token,
                     "User-Agent": "quant-system/1.0"},
        )
        try:
            with urllib.request.urlopen(req, timeout=_TIMEOUT, context=_CTX) as r:
                raw = r.read().decode("utf-8", "ignore")
        except urllib.error.HTTPError as e:
            raise DataSourceError(f"iFinD HTTP {e.code}: {e.read()[:200]!r}")
        except Exception as e:
            raise DataSourceError(f"iFinD 请求失败: {e}")

        try:
            data = json.loads(raw)
        except json.JSONDecodeError:
            raise DataSourceError(f"iFinD 返回非 JSON: {raw[:200]}")

        if data.get("errorcode") not in (0, "0", None):
            raise DataSourceError(f"iFinD errorcode={data.get('errorcode')} {data.get('errmsg')}")
        return data

    def _token(self) -> str:
        """refresh_token -> access_token（内存缓存 6 天，官方有效期 7 天）"""
        import time
        if self._access_token and time.time() - self._token_at < 6 * 86400:
            return self._access_token
        if not self._refresh_token:
            raise DataSourceError("iFinD 未配置 refresh_token")
        data = self._post("get_access_token", {}, self._refresh_token)
        tok = (data.get("data") or {}).get("access_token")
        if not tok:
            raise DataSourceError(f"iFinD 取 access_token 失败: {data}")
        self._access_token, self._token_at = tok, time.time()
        return tok

    # ------------------------------------------------------------------
    # K线
    # ------------------------------------------------------------------
    def fetch_kline(self, symbol: str, start_date: str, end_date: str,
                    period: str = "daily", adjust: str = "qfq") -> List[Dict]:
        code = std.normalize_symbol(symbol)
        payload = {
            "codes": code,
            "indicators": _KLINE_INDICATORS,
            "startdate": str(start_date)[:10],
            "enddate": str(end_date)[:10],
            "functionpara": {
                "Interval": _INTERVAL.get(period, "D"),
                "Fill": "Original",
                "Dateformat": "YYYY-MM-DD",
                "Adjustment": _ADJUSTMENT.get(adjust, "ForwardAdjust"),
            },
        }
        data = self._post("cmd_history_quotation", payload, self._token())
        return self._parse_table(data, code, kline=True, adjust=adjust)

    # ------------------------------------------------------------------
    # 实时行情
    # ------------------------------------------------------------------
    def fetch_quote(self, symbols: List[str]) -> List[Dict]:
        codes = [std.normalize_symbol(s) for s in symbols]
        if not codes:
            return []
        base = {"codes": ",".join(codes)}
        try:
            data = self._post("real_time_quotation",
                              {**base, "indicators": _QUOTE_INDICATORS}, self._token())
        except DataSourceError as e:
            # 指标权限/名称不匹配时，退到最小指标集再试一次
            logger.warning(f"[ifind] 全量指标失败（{e}），改用最小指标集重试")
            data = self._post("real_time_quotation",
                              {**base, "indicators": _QUOTE_FALLBACK}, self._token())
        return self._parse_table(data, None, kline=False, adjust="")

    # ------------------------------------------------------------------
    # 解析
    # ------------------------------------------------------------------
    def _parse_table(self, data: dict, code: Optional[str], kline: bool, adjust: str) -> List[Dict]:
        tables = data.get("tables") or []
        if isinstance(tables, dict):
            tables = [tables]

        out: List[Dict] = []
        for tb in tables:
            sym = std.normalize_symbol(tb.get("thscode") or tb.get("code") or code or "")
            tbl = tb.get("table") or {}
            times = tb.get("time") or tbl.get("time") or []
            if kline:
                n = len(times) or len(tbl.get("close") or [])
                for i in range(n):
                    row = {k: _at(v, i) for k, v in tbl.items() if isinstance(v, list)}
                    row.update({"symbol": sym, "trade_date": _at(times, i),
                                "adjust": adjust or "none"})
                    out.append({_FIELD_MAP.get(k, k): v for k, v in row.items()})
            else:
                # 实时行情：每个指标是一个长度 1 的列表
                row = {k: _first(v) for k, v in tbl.items() if isinstance(v, list)}
                row["symbol"] = sym
                row["quote_time"] = _at(times, 0) or datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                out.append({_FIELD_MAP.get(k, k): v for k, v in row.items()})
        return out

    # ------------------------------------------------------------------
    # 健康检查
    # ------------------------------------------------------------------
    def healthcheck(self) -> bool:
        if not self.enabled:
            return False
        try:
            self._token()
            return True
        except DataSourceError:
            return False


def _at(seq, i):
    if not isinstance(seq, (list, tuple)) or i >= len(seq):
        return None
    return seq[i]


def _first(seq):
    v = _at(seq, 0)
    return v


def probe() -> int:
    """命令行自检：python -m datasources.ifind --probe"""
    logging.basicConfig(level=logging.INFO)
    src = IfindSource()
    print(f"refresh_token: {'已配置' if src.enabled else '未配置（IFIND_REFRESH_TOKEN）'}")
    if not src.enabled:
        print("跳过探测。配置后重试：export IFIND_REFRESH_TOKEN=xxxx")
        return 2
    try:
        print("换取 access_token ...", "OK" if src.healthcheck() else "失败")
        rows = src.fetch_kline("600519.SH", "2026-09-01", "2026-10-19", "daily", "qfq")
        print(f"K线 600519.SH: {len(rows)} 条")
        if rows:
            print("  首行:", {k: rows[0].get(k) for k in ("trade_date", "open", "close", "volume", "amount")})
        q = src.fetch_quote(["600519.SH", "000001.SZ"])
        print(f"实时行情: {len(q)} 条")
        if q:
            print("  首条:", {k: q[0].get(k) for k in ("symbol", "price", "pct_change", "volume")})
        return 0
    except DataSourceError as e:
        print("探测失败:", e)
        return 1


if __name__ == "__main__":
    import sys
    sys.exit(probe())
