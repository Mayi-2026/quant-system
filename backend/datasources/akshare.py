"""AKShare 数据源适配器（K线补充/交叉核对 + 股票列表）

AKShare 本质是爬虫聚合库，接口随源站改版可能失效，故不设为主源，
仅作为东财主源失败时的 K线兜底，以及股票列表来源。
原始单位：volume=手，amount=元
"""
from typing import Dict, List

from .base import BaseDataSource, DataSourceError
from . import standards as std

_PERIOD_MAP = {"daily": "daily", "weekly": "weekly", "monthly": "monthly"}


def _f(v):
    try:
        return float(v)
    except (ValueError, TypeError):
        return None


class AkshareSource(BaseDataSource):
    source_name = "akshare"
    capabilities = {"kline", "stock_list"}
    raw_units = {"volume": "手", "amount": "元"}

    def _ak(self):
        try:
            import akshare as ak
            return ak
        except ImportError as e:
            raise DataSourceError(f"akshare 未安装: {e}")

    def fetch_kline(self, symbol, start_date, end_date, period="daily", adjust="qfq"):
        ak = self._ak()
        code = symbol.split(".")[0]
        try:
            df = ak.stock_zh_a_hist(
                symbol=code, period=_PERIOD_MAP.get(period, "daily"),
                start_date=start_date.replace("-", ""), end_date=end_date.replace("-", ""),
                adjust=adjust,
            )
        except Exception as e:
            raise DataSourceError(f"akshare K线失败: {e}")
        if df is None or df.empty:
            raise DataSourceError(f"akshare 返回空K线: {symbol}")

        out = []
        for _, row in df.iterrows():
            out.append({
                "symbol": std.normalize_symbol(symbol),
                "trade_date": str(row.get("日期")),
                "open": _f(row.get("开盘")), "close": _f(row.get("收盘")),
                "high": _f(row.get("最高")), "low": _f(row.get("最低")),
                "volume": _f(row.get("成交量")),     # 手
                "amount": _f(row.get("成交额")),     # 元
                "amplitude": _f(row.get("振幅")), "pct_change": _f(row.get("涨跌幅")),
                "change": _f(row.get("涨跌额")), "turnover": _f(row.get("换手率")),
                "adjust": adjust,
            })
        return out

    def fetch_stock_list(self) -> List[Dict]:
        ak = self._ak()
        try:
            df = ak.stock_info_a_code_name()
        except Exception as e:
            raise DataSourceError(f"akshare 股票列表失败: {e}")
        out = []
        for _, row in df.iterrows():
            code = str(row["code"]).zfill(6)
            name = str(row["name"])
            out.append({
                "symbol": std.normalize_symbol(code),
                "name": name,
                "market": "A股",
                "is_st": 1 if "ST" in name.upper() else 0,
            })
        if not out:
            raise DataSourceError("akshare 返回空股票列表")
        return out

    def healthcheck(self) -> bool:
        try:
            self._ak()
            return True
        except Exception:
            return False