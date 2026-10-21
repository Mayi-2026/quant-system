"""BaoStock 数据获取模块（独立测试用，仿照原系统的函数签名）

特点：
  - 免注册、免 Token，服务端推送（非爬虫），稳定不封 IP
  - 覆盖：日/周/月线、复权因子、估值(PE/PB/PS/PCF)、财务、指数成分、交易日历
  - 前复权 adjustflag: 1=后复权 2=前复权 3=不复权

注意：BaoStock 单次会话非线程安全；本模块每个函数自包含 login/logout，便于独立调用。
"""
from typing import List, Dict, Optional
import baostock as bs


def to_baostock_code(symbol: str) -> str:
    """600519 / 600519.SH / sh600519 -> sh.600519"""
    s = symbol.replace(".SH", "").replace(".SZ", "").replace("SH", "").replace("SZ", "")
    s = s.replace("sh", "").replace("sz", "")
    code = "".join(ch for ch in s if ch.isdigit())
    if code.startswith(("6", "5", "9")):   # 沪市/ETF/科创板
        return f"sh.{code}"
    return f"sz.{code}"


_FREQ_MAP = {"daily": "d", "d": "d", "weekly": "w", "w": "w", "monthly": "m", "m": "m",
             "5": "5", "15": "15", "30": "30", "60": "60"}
_ADJ_MAP = {"qfq": "2", "hfq": "1", "": "3", "none": "3"}


def _rows(rs) -> List[List[str]]:
    data = []
    while (rs.error_code == "0") and rs.next():
        data.append(rs.get_row_data())
    return data


def fetch_kline_baostock(symbol: str, start_date: str, end_date: str,
                         frequency: str = "d", adjust: str = "qfq") -> List[Dict]:
    """获取 K线（含成交额/换手率/涨跌幅/估值）

    frequency: daily/weekly/monthly 或 d/w/m/5/15/30/60
    adjust: qfq(前复权)/hfq(后复权)/空(不复权)
    """
    code = to_baostock_code(symbol)
    freq = _FREQ_MAP.get(frequency, "d")
    adj = _ADJ_MAP.get(adjust, "2")

    lg = bs.login()
    if lg.error_code != "0":
        raise RuntimeError(f"BaoStock 登录失败: {lg.error_msg}")
    try:
        # 日线可附带 preclose 与估值字段；周/月/分钟线不支持 preclose 与估值字段
        if freq == "d":
            fields = ("date,code,open,high,low,close,preclose,volume,amount,"
                      "adjustflag,turn,tradestatus,pctChg,isST,peTTM,pbMRQ,psTTM,pcfNcfTTM")
        else:
            fields = ("date,code,open,high,low,close,volume,amount,"
                      "adjustflag,turn,tradestatus,pctChg,isST")
        rs = bs.query_history_k_data_plus(
            code, fields, start_date=start_date, end_date=end_date,
            frequency=freq, adjustflag=adj,
        )
        if rs.error_code != "0":
            raise RuntimeError(f"查询失败: {rs.error_msg}")

        out = []
        for r in _rows(rs):
            d = dict(zip(rs.fields, r))
            out.append({
                "date": d.get("date"),
                "open": _f(d.get("open")), "high": _f(d.get("high")),
                "low": _f(d.get("low")), "close": _f(d.get("close")),
                "prev_close": _f(d.get("preclose")),
                "volume": _f(d.get("volume")), "amount": _f(d.get("amount")),
                "turn": _f(d.get("turn")), "pct_change": _f(d.get("pctChg")),
                "peTTM": _f(d.get("peTTM")), "pbMRQ": _f(d.get("pbMRQ")),
                "psTTM": _f(d.get("psTTM")), "pcfNcfTTM": _f(d.get("pcfNcfTTM")),
                "isST": d.get("isST"), "tradestatus": d.get("tradestatus"),
            })
        return out
    finally:
        bs.logout()


def fetch_valuation_baostock(symbol: str, start_date: str, end_date: str) -> List[Dict]:
    """获取估值时序（PE-TTM / PB-MRQ / PS-TTM / PCF-TTM）"""
    code = to_baostock_code(symbol)
    lg = bs.login()
    if lg.error_code != "0":
        raise RuntimeError(f"BaoStock 登录失败: {lg.error_msg}")
    try:
        rs = bs.query_history_k_data_plus(
            code, "date,code,close,peTTM,pbMRQ,psTTM,pcfNcfTTM",
            start_date=start_date, end_date=end_date, frequency="d", adjustflag="3",
        )
        out = []
        for r in _rows(rs):
            d = dict(zip(rs.fields, r))
            out.append({"date": d["date"], "close": _f(d.get("close")),
                        "peTTM": _f(d.get("peTTM")), "pbMRQ": _f(d.get("pbMRQ")),
                        "psTTM": _f(d.get("psTTM")), "pcfNcfTTM": _f(d.get("pcfNcfTTM"))})
        return out
    finally:
        bs.logout()


def fetch_adj_factor_baostock(symbol: str, start_date: str, end_date: str) -> List[Dict]:
    """获取复权因子（分红送股导致的复权调整）"""
    code = to_baostock_code(symbol)
    lg = bs.login()
    if lg.error_code != "0":
        raise RuntimeError(f"BaoStock 登录失败: {lg.error_msg}")
    try:
        rs = bs.query_adjust_factor(code=code, start_date=start_date, end_date=end_date)
        out = []
        for r in _rows(rs):
            d = dict(zip(rs.fields, r))
            out.append({"date": d.get("dividOperateDate"),
                        "foreAdjustFactor": _f(d.get("foreAdjustFactor")),
                        "backAdjustFactor": _f(d.get("backAdjustFactor")),
                        "adjustFactor": _f(d.get("adjustFactor"))})
        return out
    finally:
        bs.logout()


def fetch_stock_list_baostock(day: Optional[str] = None) -> List[Dict]:
    """获取全部A股列表（用于全市场同步）"""
    lg = bs.login()
    if lg.error_code != "0":
        raise RuntimeError(f"BaoStock 登录失败: {lg.error_msg}")
    try:
        rs = bs.query_all_stock(day=day) if day else bs.query_all_stock()
        out = []
        for r in _rows(rs):
            d = dict(zip(rs.fields, r))
            out.append({"code": d.get("code"), "name": d.get("code_name"),
                        "tradeStatus": d.get("tradeStatus")})
        return out
    finally:
        bs.logout()


def fetch_trade_dates_baostock(start_date: str, end_date: str) -> List[Dict]:
    """交易日历"""
    lg = bs.login()
    if lg.error_code != "0":
        raise RuntimeError(f"BaoStock 登录失败: {lg.error_msg}")
    try:
        rs = bs.query_trade_dates(start_date=start_date, end_date=end_date)
        return [dict(zip(rs.fields, r)) for r in _rows(rs)]
    finally:
        bs.logout()


def _f(v) -> Optional[float]:
    if v is None or v == "":
        return None
    try:
        return float(v)
    except (ValueError, TypeError):
        return None