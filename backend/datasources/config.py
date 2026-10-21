"""统一数据源配置（Single Source of Truth）

本文件是**两个系统共用的数据源优先级唯一来源**：
  - quant-system 从 base 读这里定义的 SOURCE_PRIORITY 构建路由表；
  - vibe-trading 通过 bridge 同步脚本，把这里的顺序写成 MARKET_DATA_ORDER_* 环境变量。

想换源 / 调优先级，**只改这一个文件**，然后跑 `python -m datasources.bridge sync-env`
（或调 API /api/data/bridge/sync），两边同时生效。

设计说明：
  - SOURCE_PRIORITY 里出现的名字须是两边**共有的**数据源，才能同步给 vibe；
  - quant 独有、vibe 没有的源（如 sina），不能写进 vibe 的链（vibe 只允许重排默认链），
    对 vibe 而言该部分数据由 Data Bridge 直接供给。
"""
from typing import Dict, List

# ---------------------------------------------------------------------------
# 数据源优先级（按能力）—— 唯一来源
# 顺序即优先级：靠前优先使用，失败自动降级到下一个。
# ---------------------------------------------------------------------------
SOURCE_PRIORITY: Dict[str, List[str]] = {
    # 历史K线：东财最快最全（实测 5.7 年 ~1.7s）；
    # iFinD（QuantAPI）数据最规范（含成交额/换手率/可复权），但按账号配额计费，
    #   置于东财之后作为高质量备源；未配置 IFIND_REFRESH_TOKEN 时自动跳过；
    # 新浪次之（实测日/周/月线可用、含 ETF，不复权）；
    # 腾讯、AKShare、baostock 依次兜底。
    # 注：2026-10 起腾讯 fqkline 接口返回 501、东财 push2his 间歇性拒连，
    #     新浪已成为最稳的 K线兜底源，故置于 tencent 之前。
    "kline": ["eastmoney", "ifind", "sina", "tencent", "akshare", "baostock"],
    # 实时行情：东财主，iFinD 次之（含 preClose/换手/PE/PB，字段更全），腾讯、新浪兜底
    "quote": ["eastmoney", "ifind", "tencent", "sina"],
    # 全市场快照：东财
    "all_spot": ["eastmoney"],
    # 指数：东财主，新浪兜底
    "indices": ["eastmoney", "sina"],
    # 股票列表：东财主，AKShare 兜底
    "stock_list": ["eastmoney", "akshare"],
}

# 说明：kline 链里 tencent 已实现 K线能力（fqkline 接口）；若无成交额需求可将其置前，
# 但东财含 amount/turnover，默认仍以东财优先。

# ---------------------------------------------------------------------------
# vibe-trading 市场名 -> quant 能力名 的映射
# 用于把 SOURCE_PRIORITY 翻译成 vibe 的 MARKET_DATA_ORDER_<MARKET> 环境变量。
# ---------------------------------------------------------------------------
VIBE_MARKET_BY_CAPABILITY: Dict[str, str] = {
    "kline": "A_SHARE",
}

# vibe 默认 A股链（须与 vibe agent/backtest/loaders/registry.py 的
# FALLBACK_CHAINS["a_share"] 保持一致）。同步时只能对默认链做**重排**，
# 不能增删源；交集之外的源引不进去。
VIBE_DEFAULT_A_SHARE_CHAIN: List[str] = [
    "tencent", "mootdx", "eastmoney", "baostock",
    "akshare", "tushare", "gildata", "local",
]

# quant 数据源名 -> vibe 数据源名（大部分同名；无对应则不参与 vibe 同步）
QUANT_TO_VIBE_SOURCE: Dict[str, str] = {
    "eastmoney": "eastmoney",
    "tencent": "tencent",
    "akshare": "akshare",
    "baostock": "baostock",
    "sina": "sina",
}


def vibe_source_order(capability: str = "kline") -> List[str]:
    """把 quant 的某能力优先级，换算成 vibe 的合法数据源顺序（重排默认链）。

    - 仅保留 quant 与 vibe 都有的源；
    - 结果必须是 vibe 默认链的**排列**（不漏不重），才能通过 vibe 校验；
    - quant 有而 vibe 没有的源被忽略；vibe 有而 quant 没提的源，按默认顺序补在后面。
    """
    default = VIBE_DEFAULT_A_SHARE_CHAIN
    quant_chain = SOURCE_PRIORITY.get(capability, [])

    # quant 顺序中、且 vibe 也认的源，映射成 vibe 名
    preferred = []
    for name in quant_chain:
        vibe_name = QUANT_TO_VIBE_SOURCE.get(name)
        if vibe_name and vibe_name in default and vibe_name not in preferred:
            preferred.append(vibe_name)

    # 其余 vibe 源按默认顺序补在尾部，凑成完整排列
    rest = [s for s in default if s not in preferred]
    return preferred + rest


def describe() -> str:
    lines = ["# 统一数据源优先级（唯一来源）", ""]
    for cap, chain in SOURCE_PRIORITY.items():
        lines.append(f"- **{cap}**: {' → '.join(chain)}")
    lines.append("")
    lines.append("## 同步给 vibe-trading 的 A股链顺序")
    lines.append(" → ".join(vibe_source_order("kline")))
    return "\n".join(lines)