"""数据源注册表（可配置路由）

把「每个能力用哪些数据源、优先级如何」集中声明。
**优先级定义已收敛到 config.py 的 SOURCE_PRIORITY（两个系统共用的唯一来源）**，
本文件只负责：实例化数据源 + 按 config 构建路由表 + 提供查询。

未来新增/替换数据源，只需：
  1. 写一个 BaseDataSource 子类适配器；
  2. 在 config.SOURCE_PRIORITY 里把它加进对应能力的优先级列表（或改顺序）；
业务代码（data_service）完全不用动。
"""
from typing import Dict, List

from .base import BaseDataSource
from .config import SOURCE_PRIORITY
from .eastmoney import EastmoneySource
from .tencent import TencentSource
from .sina import SinaSource
from .akshare import AkshareSource
from .ifind import IfindSource

# ---------------------------------------------------------------------------
# 数据源实例注册（key = source_name）
# ---------------------------------------------------------------------------
SOURCES: Dict[str, BaseDataSource] = {
    "eastmoney": EastmoneySource(),
    "tencent": TencentSource(),
    "sina": SinaSource(),
    "akshare": AkshareSource(),
    # iFinD：未配置 IFIND_REFRESH_TOKEN 时 supports() 恒为 False，路由自动跳过
    "ifind": IfindSource(),
}

# ---------------------------------------------------------------------------
# 路由表：能力 -> 数据源优先级列表
# 直接引用 config.SOURCE_PRIORITY（唯一来源）。改优先级请改 config.py。
# ---------------------------------------------------------------------------
ROUTES: Dict[str, List[str]] = {k: list(v) for k, v in SOURCE_PRIORITY.items()}


def get_sources(capability: str) -> List[BaseDataSource]:
    """按优先级返回某能力的数据源实例列表"""
    names = ROUTES.get(capability, [])
    return [SOURCES[n] for n in names if n in SOURCES]


def register_source(name: str, source: BaseDataSource, capability: str = None,
                    priority: int = None):
    """运行时注册新数据源（可选能力/优先级）"""
    SOURCES[name] = source
    if capability:
        chain = ROUTES.setdefault(capability, [])
        if name in chain:
            chain.remove(name)
        if priority is None:
            chain.append(name)
        else:
            chain.insert(max(0, priority), name)


def describe_routes() -> str:
    """人类可读的路由配置说明"""
    lines = ["# 数据源路由（DataSource Routes）", ""]
    for cap, names in ROUTES.items():
        chain = " → ".join(names)
        lines.append(f"- **{cap}**: {chain}")
    lines.append("")
    lines.append("## 已注册数据源")
    lines.append("| 数据源 | 支持能力 | volume原始单位 | amount原始单位 |")
    lines.append("|--------|----------|---------------|---------------|")
    for name, src in SOURCES.items():
        caps = "/".join(sorted(src.capabilities))
        lines.append(f"| {name} | {caps} | {src.raw_units.get('volume','-')} | {src.raw_units.get('amount','-')} |")
    return "\n".join(lines)