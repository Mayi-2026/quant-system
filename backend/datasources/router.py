"""数据源路由核心

按 registry 声明的优先级，依次尝试数据源，第一个成功即返回并标准化；
全部失败则返回空（或抛错）。是业务代码获取数据的统一入口。

特点：
  - 自动降级：主源失败自动切下一个源，无需业务层关心
  - 统一标准化：不论哪个源成功，返回的都是「标准口径」数据
  - 可观测：记录实际命中的数据源，供前端/日志展示

用法：
    from datasources.router import data_router
    res = data_router.get_kline("600519.SH", "2020-01-01", "2026-10-13")
    res["data"]        # 标准K线
    res["source"]      # 实际命中的数据源，如 "eastmoney"
    res["attempts"]    # 尝试记录 [{source, ok, error}]
"""
import logging
from typing import Dict, List, Optional

from . import registry
from . import standards as std
from .base import DataSourceError
from .standardizer import (standardize_kline, standardize_quote,
                           standardize_stock_info, standardize_index)

logger = logging.getLogger(__name__)


class DataRouter:
    def _try_chain(self, capability: str, call, standardize,
                   empty_ok: bool = False) -> Dict:
        """通用路由：按优先级尝试，返回 {data, source, attempts}"""
        attempts = []
        for src in registry.get_sources(capability):
            if not src.supports(capability):
                continue
            try:
                raw = call(src)
            except DataSourceError as e:
                attempts.append({"source": src.source_name, "ok": False, "error": str(e)})
                logger.warning(f"[router] {capability} 源 {src.source_name} 失败: {e}")
                continue
            except Exception as e:
                attempts.append({"source": src.source_name, "ok": False, "error": f"{type(e).__name__}: {e}"})
                logger.warning(f"[router] {capability} 源 {src.source_name} 异常: {e}")
                continue

            std_result = standardize(raw, src.source_name)
            data = std_result["data"] if isinstance(std_result, dict) else std_result
            if not data and not empty_ok:
                attempts.append({"source": src.source_name, "ok": False, "error": "标准化后为空"})
                continue

            attempts.append({"source": src.source_name, "ok": True})
            return {"data": data, "source": src.source_name, "attempts": attempts}

        logger.error(f"[router] {capability} 所有数据源均失败: {attempts}")
        return {"data": [], "source": None, "attempts": attempts}

    # ---- 对外能力接口 ----
    def get_kline(self, symbol: str, start_date: str, end_date: str,
                  period: str = "daily", adjust: str = "qfq") -> Dict:
        return self._try_chain(
            "kline",
            lambda s: s.fetch_kline(symbol, start_date, end_date, period, adjust),
            lambda raw, name: standardize_kline(raw, name),
        )

    def get_quote(self, symbols: List[str]) -> Dict:
        return self._try_chain(
            "quote",
            lambda s: s.fetch_quote(symbols),
            lambda raw, name: standardize_quote(raw, name),
        )

    def get_all_spot(self) -> Dict:
        return self._try_chain(
            "all_spot",
            lambda s: s.fetch_all_spot(),
            lambda raw, name: standardize_quote(raw, name),
        )

    def get_indices(self) -> Dict:
        return self._try_chain(
            "indices",
            lambda s: s.fetch_indices(),
            lambda raw, name: standardize_index(raw),
        )

    def get_stock_list(self) -> Dict:
        return self._try_chain(
            "stock_list",
            lambda s: s.fetch_stock_list(),
            lambda raw, name: standardize_stock_info(raw),
        )


# 全局单例
data_router = DataRouter()