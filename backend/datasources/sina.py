"""新浪财经数据源适配器（实时行情 + 指数 + 历史K线 兜底）

接口：
  实时行情  hq.sinajs.cn/list=sh600519,sz000001
  历史K线   quotes.sina.cn/cn/api/jsonp_v2.php/.../CN_MarketDataService.getKLineData
原始单位：volume=股，amount=元
注意：必须带 Referer，否则返回 403；行情返回 GBK 编码，K线返回 JSONP。
K线接口返回**不复权**数据（接口不提供 fq 参数生效差异），故 adjust 仅记录、不做换算。
"""
import json
import re
import ssl
import urllib.request
from typing import Dict, List

from .base import BaseDataSource, DataSourceError
from . import standards as std

_CTX = ssl.create_default_context()
_CTX.check_hostname = False
_CTX.verify_mode = ssl.CERT_NONE
_HEADERS = {"User-Agent": "Mozilla/5.0", "Referer": "https://finance.sina.com.cn/"}

# 新浪K线周期：scale 为分钟数（日=240，周=1200，月=7200）
_KLINE_SCALE = {"daily": 240, "weekly": 1200, "monthly": 7200}


def _to_sina(symbol: str) -> str:
    code, _, exchange = std.normalize_symbol(symbol).partition(".")
    return ("sh" if exchange == "SH" else "sz") + code


def _f(v):
    try:
        return float(v)
    except (ValueError, TypeError):
        return None


class SinaSource(BaseDataSource):
    source_name = "sina"
    capabilities = {"quote", "indices", "kline"}
    raw_units = {"volume": "股", "amount": "元"}

    def _raw(self, codes: List[str]) -> str:
        url = f"https://hq.sinajs.cn/list={','.join(codes)}"
        req = urllib.request.Request(url, headers=_HEADERS)
        try:
            with urllib.request.urlopen(req, timeout=10, context=_CTX) as r:
                return r.read().decode("gbk", "ignore")
        except Exception as e:
            raise DataSourceError(f"新浪请求失败: {e}")

    def fetch_kline(self, symbol: str, start_date: str, end_date: str,
                    period: str = "daily", adjust: str = "qfq") -> List[Dict]:
        """历史K线（新浪不复权日/周/月线）。

        说明：新浪该接口不支持按日期区间过滤，只能按 datalen 取最近 N 条，
        因此这里先取一个足够大的 N，再在本地按 [start_date, end_date] 裁剪。
        返回的 volume 原始单位为「股」（与标准口径一致，standardizer 无需换算）。
        """
        scode = _to_sina(symbol)
        scale = _KLINE_SCALE.get(period, 240)
        # 取足够长的历史以便裁剪出目标区间（日线约 5.3 年，周月线取更多）
        datalen = 1400 if period == "daily" else 800
        url = (f"https://quotes.sina.cn/cn/api/jsonp_v2.php/var%20_{scode}="
               f"/CN_MarketDataService.getKLineData"
               f"?symbol={scode}&scale={scale}&ma=no&datalen={datalen}")
        req = urllib.request.Request(url, headers=_HEADERS)
        try:
            with urllib.request.urlopen(req, timeout=15, context=_CTX) as r:
                text = r.read().decode("utf-8", "ignore")
        except Exception as e:
            raise DataSourceError(f"新浪K线请求失败: {e}")

        # 剥离 JSONP 外壳：var _sh600519=([...]);
        m = re.search(r"=\((\[.*?\])\)", text, re.S) or re.search(r"=(\[.*?\])", text, re.S)
        if not m:
            raise DataSourceError(f"新浪K线解析失败: {symbol}")
        try:
            rows = json.loads(m.group(1))
        except json.JSONDecodeError as e:
            raise DataSourceError(f"新浪K线JSON解析失败: {e}")

        norm = std.normalize_symbol(symbol)
        out: List[Dict] = []
        prev_close = None
        for row in rows:
            d = row.get("day")
            if not d:
                continue
            if start_date and d < start_date:
                prev_close = _f(row.get("close")) or prev_close
                continue
            if end_date and d > end_date:
                continue
            close = _f(row.get("close"))
            pct = None
            if prev_close and close and prev_close > 0:
                pct = round((close / prev_close - 1) * 100, 4)
            out.append({
                "symbol": norm,
                "trade_date": d,
                "open": _f(row.get("open")), "close": close,
                "high": _f(row.get("high")), "low": _f(row.get("low")),
                "volume": _f(row.get("volume")),   # 股（标准单位）
                "amount": None,                    # 新浪K线不含成交额
                "pct_change": pct,
                "pre_close": prev_close,
                "adjust": adjust,
            })
            prev_close = close
        if not out:
            raise DataSourceError(f"新浪返回空K线: {symbol}")
        return out

    def fetch_quote(self, symbols: List[str]) -> List[Dict]:
        raw = self._raw([_to_sina(s) for s in symbols])
        out = []
        for line in raw.split("\n"):
            m = re.search(r'="([^"]+)"', line)
            if not m:
                continue
            f = m.group(1).split(",")
            if len(f) < 32:
                continue
            var = line.split("=")[0].split("_")[-1]
            out.append({
                "symbol": std.normalize_symbol(var, "SH" if var.startswith("sh") else "SZ"),
                "name": f[0],
                "open": _f(f[1]), "pre_close": _f(f[2]), "price": _f(f[3]),
                "high": _f(f[4]), "low": _f(f[5]),
                "volume": _f(f[8]),          # 股
                "amount": _f(f[9]),          # 元
                "quote_time": f"{f[30]} {f[31]}" if len(f) > 31 else None,
            })
        # 补算涨跌幅
        for q in out:
            if q["price"] and q["pre_close"]:
                q["change"] = round(q["price"] - q["pre_close"], 4)
                q["pct_change"] = round(q["change"] / q["pre_close"] * 100, 4)
        if not out:
            raise DataSourceError(f"新浪返回空行情: {symbols}")
        return out

    def fetch_indices(self) -> List[Dict]:
        indices = [
            ("上证指数", "sh000001"), ("深证成指", "sz399001"), ("创业板指", "sz399006"),
            ("沪深300", "sh000300"), ("科创50", "sh000688"), ("创业板50", "sz399673"),
            ("中证500", "sh000905"),
            ("中证1000", "sh000852"),
        ]
        raw = self._raw([c for _, c in indices])
        by_code = {}
        for line in raw.split("\n"):
            m = re.search(r'="([^"]+)"', line)
            if not m:
                continue
            code = line.split("=")[0].split("_")[-1]
            f = m.group(1).split(",")
            if len(f) >= 4:
                by_code[code] = f
        out = []
        for name, code in indices:
            f = by_code.get(code)
            if not f:
                continue
            # 新浪指数格式：名称,今开,昨收,最新价,最高,最低,...
            # 最新价（收盘）在 f[3]，f[1] 是今日开盘价，不能当收盘价用。
            close, pre = _f(f[3]), _f(f[2])
            change = (close - pre) if (close and pre) else None
            out.append({"symbol": code, "name": name, "value": close,
                        "change": round(change, 2) if change is not None else None,
                        "change_pct": round(change / pre * 100, 2) if (change is not None and pre) else None})
        if not out:
            raise DataSourceError("新浪返回空指数")
        return out

    def healthcheck(self) -> bool:
        try:
            self.fetch_quote(["600519.SH"])
            return True
        except Exception:
            return False