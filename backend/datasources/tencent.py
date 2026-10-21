"""腾讯行情数据源适配器（实时行情 + 历史K线）

接口：
  实时行情  qt.gtimg.cn/q=sh600519,sz000001
  历史K线   web.ifzq.gtimg.cn/appstock/app/fqkline/get
原始单位：volume=手，amount=万元
注意：实时接口返回 GBK 编码，文本用 ~ 分隔；K线接口返回 JSON。
"""
import json
import ssl
import urllib.request
from typing import Dict, List

from .base import BaseDataSource, DataSourceError
from . import standards as std

_CTX = ssl.create_default_context()
_CTX.check_hostname = False
_CTX.verify_mode = ssl.CERT_NONE


def _f(v):
    try:
        return float(v)
    except (ValueError, TypeError):
        return None


def _to_tencent(symbol: str) -> str:
    code, _, exchange = std.normalize_symbol(symbol).partition(".")
    return ("sh" if exchange == "SH" else "sz") + code


# 腾讯K线周期代号
_KLINE_PERIOD = {"daily": "day", "weekly": "week", "monthly": "month"}
# 复权代号（腾讯 fqkline 的 param 末位）
_KLINE_FQ = {"qfq": "qfq", "hfq": "hfq", "none": ""}


class TencentSource(BaseDataSource):
    source_name = "tencent"
    capabilities = {"quote", "kline"}
    raw_units = {"volume": "手", "amount": "万元"}

    def fetch_kline(self, symbol, start_date, end_date, period="daily", adjust="qfq"):
        tcode = _to_tencent(symbol)
        p = _KLINE_PERIOD.get(period, "day")
        fq = _KLINE_FQ.get(adjust, "qfq")
        url = (f"https://web.ifzq.gtimg.cn/appstock/app/fqkline/get?"
               f"param={tcode},{p},{start_date},{end_date},640,{fq}")
        req = urllib.request.Request(url, headers={
            "User-Agent": "Mozilla/5.0", "Referer": "https://gu.qq.com/"})
        try:
            with urllib.request.urlopen(req, timeout=15, context=_CTX) as r:
                data = json.loads(r.read().decode("utf-8"))
        except Exception as e:
            raise DataSourceError(f"腾讯K线请求失败: {e}")

        node = (data.get("data") or {}).get(tcode) or {}
        # 前复权在 qfqday，不复权在 day，后复权在 hfqday
        klines = node.get("qfqday") or node.get("hfqday") or node.get("day") or []
        out = []
        prev_close = None
        for row in klines:
            if len(row) < 6:
                continue
            date_s, o, c, h, l, v = row[0], row[1], row[2], row[3], row[4], row[5]
            close = _f(c)
            pct = None
            if prev_close and close and prev_close > 0:
                pct = round((close / prev_close - 1) * 100, 4)
            out.append({
                "symbol": std.normalize_symbol(symbol),
                "trade_date": date_s,
                "open": _f(o), "close": close, "high": _f(h), "low": _f(l),
                "volume": _f(v),          # 手 -> 标准层转股
                "amount": None,           # 腾讯K线不含成交额
                "pct_change": pct,
                "pre_close": prev_close,
                "adjust": adjust,
            })
            prev_close = close
        if not out:
            raise DataSourceError(f"腾讯返回空K线: {symbol}")
        return out

    def fetch_quote(self, symbols: List[str]) -> List[Dict]:
        codes = ",".join(_to_tencent(s) for s in symbols)
        url = f"https://qt.gtimg.cn/q={codes}"
        req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
        try:
            with urllib.request.urlopen(req, timeout=10, context=_CTX) as r:
                raw = r.read().decode("gbk", "ignore")
        except Exception as e:
            raise DataSourceError(f"腾讯请求失败: {e}")

        out = []
        for line in raw.split(";"):
            if "~" not in line or '"' not in line:
                continue
            f = line.split('"')[1].split("~")
            if len(f) < 46:
                continue
            out.append({
                "symbol": std.normalize_symbol(f[2], "SH" if f[2].startswith(("6", "5", "9")) else "SZ"),
                "name": f[1],
                "price": _f(f[3]), "pre_close": _f(f[4]), "open": _f(f[5]),
                "volume": _f(f[6]),          # 手
                "amount": _f(f[37]),         # 万元
                "turnover": _f(f[38]), "pe": _f(f[39]),
                "high": _f(f[33]), "low": _f(f[34]),
                "pct_change": _f(f[32]), "change": _f(f[31]),
                "pb": _f(f[46]) if len(f) > 46 else None,
                "quote_time": f[30] if len(f) > 30 else None,
            })
        if not out:
            raise DataSourceError(f"腾讯返回空行情: {symbols}")
        return out

    def healthcheck(self) -> bool:
        try:
            self.fetch_quote(["600519.SH"])
            return True
        except Exception:
            return False