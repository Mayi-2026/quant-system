"""东方财富数据源适配器（历史K线主源 + 实时行情主源）

接口：
  历史K线  push2his.eastmoney.com/api/qt/stock/kline/get
  批量行情 push2.eastmoney.com/api/qt/ulist.np/get
  全市场   push2.eastmoney.com/api/qt/clist/get

原始单位：volume=手，amount=元（由 standardizer 换算为「股」「元」）
注意：实时接口必须带 fltt=2，否则价格放大 100 倍；全市场 clist 每页封顶 100 只需分页。
"""
import json
import ssl
import urllib.request
from typing import Dict, List

from .base import BaseDataSource, DataSourceError
from . import standards as std

_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36",
    "Referer": "https://quote.eastmoney.com/",
}
_CTX = ssl.create_default_context()
_CTX.check_hostname = False
_CTX.verify_mode = ssl.CERT_NONE

_KLT = {"1m": 1, "5m": 5, "15m": 15, "30m": 30, "60m": 60,
        "daily": 101, "weekly": 102, "monthly": 103}
_FQT = {"none": 0, "qfq": 1, "hfq": 2}


def _get_json(url: str, timeout: int = 10) -> dict:
    req = urllib.request.Request(url, headers=_HEADERS)
    try:
        with urllib.request.urlopen(req, timeout=timeout, context=_CTX) as r:
            return json.loads(r.read().decode("utf-8"))
    except Exception as e:
        raise DataSourceError(f"东财请求失败: {e}")


def _to_secid(symbol: str) -> str:
    """600519.SH -> 1.600519 ; 000001.SZ -> 0.000001

    优先用代码里的交易所后缀判断（指数如 000300.SH 必须是沪市 1.，不能按 0 开头误判成深市），
    无后缀时才按代码前缀推断。
    """
    norm = std.normalize_symbol(symbol)
    parts = norm.split(".")
    code = parts[0]
    exchange = parts[1] if len(parts) > 1 else ""
    if exchange == "SH":
        market = "1"
    elif exchange in ("SZ", "BJ"):
        market = "0"
    else:
        market = "1" if code.startswith(("6", "5", "9")) else "0"
    return f"{market}.{code}"


def _f(v):
    if v in (None, "", "-"):
        return None
    try:
        return float(v)
    except (ValueError, TypeError):
        return None


class EastmoneySource(BaseDataSource):
    source_name = "eastmoney"
    capabilities = {"kline", "quote", "all_spot", "indices", "stock_list"}
    raw_units = {"volume": "手", "amount": "元"}

    def fetch_kline(self, symbol, start_date, end_date, period="daily", adjust="qfq"):
        secid = _to_secid(symbol)
        klt = _KLT.get(period, 101)
        fqt = _FQT.get(adjust, 1)
        beg = start_date.replace("-", "")
        end = end_date.replace("-", "")
        url = (f"https://push2his.eastmoney.com/api/qt/stock/kline/get?secid={secid}"
               f"&fields1=f1,f2,f3,f4,f5,f6"
               f"&fields2=f51,f52,f53,f54,f55,f56,f57,f58,f59,f60,f61"
               f"&klt={klt}&fqt={fqt}&beg={beg}&end={end}")
        data = _get_json(url)
        klines = (data.get("data") or {}).get("klines") or []
        out = []
        for line in klines:
            p = line.split(",")
            if len(p) < 11:
                continue
            out.append({
                "symbol": std.normalize_symbol(symbol),
                "trade_date": p[0],
                "open": _f(p[1]), "close": _f(p[2]), "high": _f(p[3]), "low": _f(p[4]),
                "volume": _f(p[5]),           # 手 -> 标准层转股
                "amount": _f(p[6]),           # 元
                "amplitude": _f(p[7]), "pct_change": _f(p[8]),
                "change": _f(p[9]), "turnover": _f(p[10]),
                "adjust": adjust,
            })
        if not out:
            raise DataSourceError(f"东财返回空K线: {symbol}")
        return out

    def fetch_quote(self, symbols: List[str]) -> List[Dict]:
        secids = ",".join(_to_secid(s) for s in symbols)
        url = (f"https://push2.eastmoney.com/api/qt/ulist.np/get?secids={secids}"
               f"&fltt=2&invt=2"
               f"&fields=f2,f3,f4,f5,f6,f8,f9,f12,f14,f15,f16,f17,f18,f23,f124")
        data = _get_json(url)
        diff = (data.get("data") or {}).get("diff") or []
        if isinstance(diff, dict):
            diff = list(diff.values())
        out = []
        for it in diff:
            out.append({
                "symbol": std.normalize_symbol(it.get("f12")),
                "name": it.get("f14"),
                "price": _f(it.get("f2")), "pct_change": _f(it.get("f3")),
                "change": _f(it.get("f4")), "volume": _f(it.get("f5")),
                "amount": _f(it.get("f6")), "turnover": _f(it.get("f8")),
                "pe": _f(it.get("f9")), "high": _f(it.get("f15")), "low": _f(it.get("f16")),
                "open": _f(it.get("f17")), "pre_close": _f(it.get("f18")), "pb": _f(it.get("f23")),
            })
        if not out:
            raise DataSourceError(f"东财返回空行情: {symbols}")
        return out

    def fetch_all_spot(self) -> List[Dict]:
        base = ("https://push2.eastmoney.com/api/qt/clist/get?"
                "pn={pn}&pz=100&po=1&np=1&fltt=2&invt=2&fid=f3"
                "&fs=m:0+t:6,m:0+t:80,m:1+t:2,m:1+t:23,m:0+t:81+s:2048"
                "&fields=f2,f3,f5,f6,f8,f9,f12,f14,f23")
        out, pn = [], 1
        while pn <= 100:
            data = _get_json(base.format(pn=pn), timeout=20)
            d = data.get("data") or {}
            diff = d.get("diff") or []
            if isinstance(diff, dict):
                diff = list(diff.values())
            if not diff:
                break
            for it in diff:
                out.append({
                    "symbol": std.normalize_symbol(it.get("f12")),
                    "name": it.get("f14"),
                    "price": _f(it.get("f2")), "pct_change": _f(it.get("f3")),
                    "volume": _f(it.get("f5")), "amount": _f(it.get("f6")),
                    "turnover": _f(it.get("f8")), "pe": _f(it.get("f9")), "pb": _f(it.get("f23")),
                })
            total = d.get("total") or 0
            if len(out) >= total:
                break
            pn += 1
        if not out:
            raise DataSourceError("东财返回空全市场快照")
        return out

    def fetch_stock_list(self) -> List[Dict]:
        """全市场股票列表（含名称、代码）"""
        rows = self.fetch_all_spot()
        return [{"symbol": r["symbol"], "name": r["name"], "market": "A股"} for r in rows]

    def fetch_indices(self) -> List[Dict]:
        """主要指数（东财 secid 前缀 1.=沪 0.=深）"""
        indices = [
            ("上证指数", "1.000001"), ("深证成指", "0.399001"), ("创业板指", "0.399006"),
            ("沪深300", "1.000300"), ("科创50", "1.000688"), ("创业板50", "0.399673"),
            ("中证500", "1.000905"),
            ("中证1000", "1.000852"),
        ]
        secids = ",".join(c for _, c in indices)
        url = (f"https://push2.eastmoney.com/api/qt/ulist.np/get?secids={secids}&fltt=2&invt=2"
               f"&fields=f2,f3,f4,f12,f14")
        data = _get_json(url)
        diff = (data.get("data") or {}).get("diff") or []
        if isinstance(diff, dict):
            diff = list(diff.values())
        by_code = {str(it.get("f12")): it for it in diff}
        out = []
        for name, secid in indices:
            code = secid.split(".")[1]
            it = by_code.get(code)
            if it:
                out.append({"symbol": secid, "name": name, "value": _f(it.get("f2")),
                            "change": _f(it.get("f4")), "change_pct": _f(it.get("f3"))})
        if not out:
            raise DataSourceError("东财返回空指数")
        return out

    def healthcheck(self) -> bool:
        try:
            self.fetch_quote(["600519.SH"])
            return True
        except Exception:
            return False