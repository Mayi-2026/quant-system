"""数据源层（DataSource Layer）

统一入口：不论数据来自东财、腾讯、新浪还是 AKShare，都经过
  适配器(取原始) -> 标准化(统一口径) -> 路由(自动降级)
后，以标准格式提供给业务层。

用法：
    from datasources import data_router, standards
    res = data_router.get_kline("600519.SH", "2020-01-01", "2026-10-13")
"""
from .router import data_router, DataRouter
from . import standards
from . import registry
from . import config
from .base import BaseDataSource, DataSourceError

__all__ = ["data_router", "DataRouter", "standards", "registry", "config",
           "BaseDataSource", "DataSourceError"]