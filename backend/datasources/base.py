"""数据源适配器基类

所有数据源适配器统一实现这组接口。适配器只负责「取原始数据 + 声明原始单位」，
不做单位换算（换算交给 standardizer），保证入库口径一致。

核心接口：
  fetch_kline(symbol, start, end, period, adjust) -> List[dict]   # 原始K线
  fetch_quote(symbols) -> List[dict]                             # 原始实时行情
  fetch_all_spot() -> List[dict]                                 # 原始全市场快照（可选）
  fetch_indices() -> List[dict]                                  # 原始指数（可选）

每个适配器通过类属性声明能力与原始单位：
  capabilities: 支持的能力集合（kline / quote / all_spot / indices / stock_list）
  source_name:  数据源标识（用于日志和标准字段换算）
  raw_units:    {"volume": "手", "amount": "元"} 原始单位
"""
from abc import ABC, abstractmethod
from typing import Dict, List, Optional

from . import standards as std


class DataSourceError(Exception):
    """数据源取数失败（供路由层捕获并降级）"""


class BaseDataSource(ABC):
    source_name: str = "base"
    capabilities: set = set()
    raw_units: Dict[str, str] = {"volume": std.SOURCE_UNITS.get("eastmoney", {}).get("volume", "手"),
                                 "amount": "元"}

    # ---- 能力探测 ----
    def supports(self, capability: str) -> bool:
        return capability in self.capabilities

    # ---- 标准接口（子类按需实现，未实现则视为不支持）----
    def fetch_kline(self, symbol: str, start_date: str, end_date: str,
                    period: str = "daily", adjust: str = "qfq") -> List[Dict]:
        raise DataSourceError(f"{self.source_name} 不支持 K线")

    def fetch_quote(self, symbols: List[str]) -> List[Dict]:
        raise DataSourceError(f"{self.source_name} 不支持实时行情")

    def fetch_all_spot(self) -> List[Dict]:
        raise DataSourceError(f"{self.source_name} 不支持全市场快照")

    def fetch_indices(self) -> List[Dict]:
        raise DataSourceError(f"{self.source_name} 不支持指数")

    def fetch_stock_list(self) -> List[Dict]:
        raise DataSourceError(f"{self.source_name} 不支持股票列表")

    # ---- 生命周期钩子（可选）----
    def healthcheck(self) -> bool:
        """快速连通性探测，供路由在故障时跳过"""
        return True

    def close(self):
        """释放连接（如有）"""
        pass