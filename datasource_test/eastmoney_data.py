"""东方财富直连数据模块（推荐主源）

为什么选东财直连作为历史K线主源：
  - 纯 HTTP + JSON，无第三方库依赖（不引入 akshare 那套脆弱爬虫封装）
  - 速度快：5.7年日线 1640 条 ≈ 1.7s（对比 BaoStock 30~70s）
  - 字段完整：开高低收/成交量/成交额/振幅/涨跌幅/涨跌额/换手率
  - 支持多周期：klt=1/5/15/30/60(分钟),101(日),102(周),103(月)
  - 支持复权：fqt=0(不复权)/1(前复权)/2(后复权)

接口：
  历史K线  push2his.eastmoney.com/api/qt/stock/kline/get
  批量快照 push2.eastmoney.com/api/qt/ulist.np/get
  全市场   push2.eastmoney.com/api/qt/clist/get
"""
import json
import ssl
import urllib.request
from typing import List, Dict, Optional

_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36",
    "Referer": "https://quote.eastmoney.com/",
}
_CTX = ssl.create_default_context()
_CTX.check_hostname = False
_CTX.verify_mode = ssl.CERT_NONE

_KLT = {"1m": 1, "5m": 5, "15m": 15, "30m": 30, "60m": 60,
        "daily": 101, "d": 101, "weekly": 102, "w": 102, "monthly": 103, "m": 103}
_FQT = {"none": 0, "": 0, "qfq": 1, "hfq": 2}


def _http_json(url: str, timeout: int = 10) -> dict:
    req = urllib.request.Request(url, headers=_HEADERS)
    with urllib.request.urlopen(req, timeout=timeout, context=_CTX) as r:
        return json.loads(r.read().decode("utf-8"))


def to_secid(code: str) -> str:
    """600519 / 600519.SH / sh600519 -> 1.600519 ; 000001 -> 0.000001"""
    c = code.upper().replace("SH", "").replace("SZ", "").replace(".", "")
    digits = "".join(ch for ch in c if ch.isdigit())
    market = "1" if digits.startswith(("6", "5", "9")) else "0"
    return f"{market}.{digits}"


def fetch_kline(code: str, start_date: str, end_date: str,
                period: str = "daily", adjust: str = "qfq") -> List[Dict]:
    """获取历史K线。日期格式 YYYY-MM-DD 或 YYYYMMDD。"""
    secid = to_secid(code)
    klt = _KLT.get(period, 101)
    fqt = _FQT.get(adjust, 1)
    beg = start_date.replace("-", "")
    end = end_date.replace("-", "")
    url = (f"https://push2his.eastmoney.com/api/qt/stock/kline/get?secid={secid}"
           f"&fields1=f1,f2,f3,f4,f5,f6"
           f"&fields2=f51,f52,f53,f54,f55,f56,f57,f58,f59,f60,f61"
           f"&klt={klt}&fqt={fqt}&beg={beg}&end={end}")
    data = _http_json(url)
    klines = (data.get("data") or {}).get("klines") or []
    out = []
    for line in klines:
        p = line.split(",")
        if len(p) < 11:
            continue
        out.append({
            "date": p[0], "open": _f(p[1]), "close": _f(p[2]), "high": _f(p[3]),
            "low": _f(p[4]), "volume": _f(p[5]), "amount": _f(p[6]),
            "amplitude": _f(p[7]), "pct_change": _f(p[8]), "change": _f(p[9]),
            "turnover": _f(p[10]),
        })
    return out


def fetch_realtime(codes: List[str]) -> List[Dict]:
    """批量实时快照（一次可请求多只）。fltt=2 保证价格为真实值。"""
    secids = ",".join(to_secid(c) for c in codes)
    url = (f"https://push2.eastmoney.com/api/qt/ulist.np/get?secids={secids}"
           f"&fltt=2&invt=2"
           f"&fields=f2,f3,f4,f5,f6,f8,f9,f12,f14,f15,f16,f17,f18,f23,f124")
    data = _http_json(url)
    diff = (data.get("data") or {}).get("diff") or []
    if isinstance(diff, dict):
        diff = list(diff.values())
    return [{
        "code": it.get("f12"), "name": it.get("f14"),
        "price": _f(it.get("f2")), "pct_change": _f(it.get("f3")),
        "change": _f(it.get("f4")), "volume": _f(it.get("f5")),
        "amount": _f(it.get("f6")), "turnover": _f(it.get("f8")),
        "pe": _f(it.get("f9")), "high": _f(it.get("f15")), "low": _f(it.get("f16")),
        "open": _f(it.get("f17")), "prev_close": _f(it.get("f18")), "pb": _f(it.get("f23")),
    } for it in diff]


def fetch_all_spot() -> List[Dict]:
    """全市场实时快照。东财 clist 接口每页封顶 100 只，需分页拉取。"""
    base = ("https://push2.eastmoney.com/api/qt/clist/get?"
            "pn={pn}&pz=100&po=1&np=1&fltt=2&invt=2&fid=f3"
            "&fs=m:0+t:6,m:0+t:80,m:1+t:2,m:1+t:23,m:0+t:81+s:2048"
            "&fields=f2,f3,f5,f6,f8,f9,f12,f14,f23")
    out: List[Dict] = []
    pn = 1
    while True:
        data = _http_json(base.format(pn=pn), timeout=20)
        d = data.get("data") or {}
        diff = d.get("diff") or []
        if isinstance(diff, dict):
            diff = list(diff.values())
        if not diff:
            break
        for it in diff:
            out.append({
                "code": it.get("f12"), "name": it.get("f14"),
                "price": _f(it.get("f2")), "pct_change": _f(it.get("f3")),
                "volume": _f(it.get("f5")), "amount": _f(it.get("f6")),
                "turnover": _f(it.get("f8")), "pe": _f(it.get("f9")), "pb": _f(it.get("f23")),
            })
        total = d.get("total") or 0
        if len(out) >= total or pn > 100:
            break
        pn += 1
    return out


def _f(v) -> Optional[float]:
    if v is None or v == "" or v == "-":
        return None
    try:
        return float(v)
    except (ValueError, TypeError):
        return None