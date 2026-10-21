"""数据标准（Single Source of Truth）

本文件定义系统所有市场数据的统一口径。**任何数据源的数据，在入库前都必须
经过 standardizer 转换成本文件定义的字段、格式与单位。**

设计原则：
  1. 一个字段一个含义：名称、单位、精度唯一确定，不允许"同名不同义"。
  2. 单位显式声明：volume=股、amount=元，杜绝东财"手"与新浪"股"混用。
  3. 格式统一：日期 YYYY-MM-DD，代码 600519.SH，涨跌幅单位为 %。
  4. 可校验：每个字段带校验规则，脏数据在入库前拦截。

字段命名沿用现有数据库列名（market_data / stock_info），避免破坏现有前后端。
"""
from dataclasses import dataclass, field
from typing import Callable, Optional


# ---------------------------------------------------------------------------
# 标准枚举 / 常量
# ---------------------------------------------------------------------------
STANDARD_DATE_FMT = "%Y-%m-%d"          # 标准日期格式
STANDARD_SYMBOL_FMT = "{code}.{exchange}"  # 标准代码格式，如 600519.SH

# 市场代码
MARKET_SH = "SH"
MARKET_SZ = "SZ"
MARKET_BJ = "BJ"

# 复权类型
ADJUST_NONE = "none"   # 不复权
ADJUST_QFQ = "qfq"     # 前复权
ADJUST_HFQ = "hfq"     # 后复权

# 周期
PERIOD_DAILY = "daily"
PERIOD_WEEKLY = "weekly"
PERIOD_MONTHLY = "monthly"
MINUTE_PERIODS = ("1m", "5m", "15m", "30m", "60m")


# ---------------------------------------------------------------------------
# 字段标准定义
# ---------------------------------------------------------------------------
@dataclass
class FieldSpec:
    name: str                      # 字段名
    unit: str                      # 单位（"元" / "股" / "%" / "日期" / ""）
    dtype: str                     # 数据类型（float / str / int / date）
    precision: int                 # 小数位（价格类 4 位，比率类 4 位，金额 2 位）
    required: bool                 # 是否必填
    description: str               # 含义说明
    validate: Optional[Callable] = None  # 校验函数，返回 (ok, msg)


def _pos(v):
    return (v is None) or (v >= 0)


def _price_ok(v):
    return (v is None) or (v > 0)


def _pct_ok(v):
    return (v is None) or (-100 <= v <= 10000)


# --- K线 / 行情标准字段（对应 market_data 表）---
KLINE_FIELDS = [
    FieldSpec("symbol", "", "str", 0, True, "标准代码，如 600519.SH"),
    FieldSpec("trade_date", "日期", "date", 0, True, "交易日期，YYYY-MM-DD 或 date 对象"),
    FieldSpec("open", "元", "float", 4, True, "开盘价", _price_ok),
    FieldSpec("high", "元", "float", 4, True, "最高价", _price_ok),
    FieldSpec("low", "元", "float", 4, True, "最低价", _price_ok),
    FieldSpec("close", "元", "float", 4, True, "收盘价", _price_ok),
    # ↓↓↓ 核心口径：成交量统一为「股」，成交额统一为「元」
    FieldSpec("volume", "股", "float", 0, True, "成交量，单位=股（1手=100股）", _pos),
    FieldSpec("amount", "元", "float", 2, False, "成交额，单位=元", _pos),
    FieldSpec("turnover", "%", "float", 4, False, "换手率，单位=%", _pos),
    FieldSpec("pct_change", "%", "float", 4, False, "涨跌幅，单位=%", _pct_ok),
    FieldSpec("amplitude", "%", "float", 4, False, "振幅，单位=%", _pos),
    FieldSpec("change", "元", "float", 4, False, "涨跌额，单位=元"),
    FieldSpec("pre_close", "元", "float", 4, False, "昨收价，单位=元", _price_ok),
    FieldSpec("adjust", "", "str", 0, False, "复权类型 none/qfq/hfq"),
]

# --- 实时行情标准字段 ---
QUOTE_FIELDS = [
    FieldSpec("symbol", "", "str", 0, True, "标准代码"),
    FieldSpec("name", "", "str", 0, False, "股票名称"),
    FieldSpec("price", "元", "float", 4, True, "最新价"),
    FieldSpec("pre_close", "元", "float", 4, False, "昨收价"),
    FieldSpec("open", "元", "float", 4, False, "今开"),
    FieldSpec("high", "元", "float", 4, False, "今日最高"),
    FieldSpec("low", "元", "float", 4, False, "今日最低"),
    FieldSpec("volume", "股", "float", 0, False, "成交量，单位=股"),
    FieldSpec("amount", "元", "float", 2, False, "成交额，单位=元"),
    FieldSpec("turnover", "%", "float", 4, False, "换手率，单位=%"),
    FieldSpec("pct_change", "%", "float", 4, False, "涨跌幅，单位=%"),
    FieldSpec("change", "元", "float", 4, False, "涨跌额，单位=元"),
    FieldSpec("pe", "", "float", 4, False, "市盈率 TTM"),
    FieldSpec("pb", "", "float", 4, False, "市净率 MRQ"),
    FieldSpec("quote_time", "日期时间", "str", 0, False, "行情时间"),
]

# --- 股票基本信息标准字段（对应 stock_info 表）---
STOCK_INFO_FIELDS = [
    FieldSpec("symbol", "", "str", 0, True, "标准代码，如 600519.SH"),
    FieldSpec("name", "", "str", 0, True, "股票名称"),
    FieldSpec("market", "", "str", 0, False, "市场 A股/港股/美股"),
    FieldSpec("exchange", "", "str", 0, False, "交易所 SH/SZ/BJ"),
    FieldSpec("industry", "", "str", 0, False, "行业"),
    FieldSpec("list_date", "日期", "date", 0, False, "上市日期"),
    FieldSpec("is_st", "", "int", 0, False, "是否ST 0/1"),
]

# --- 指数标准字段 ---
INDEX_FIELDS = [
    FieldSpec("symbol", "", "str", 0, True, "指数代码"),
    FieldSpec("name", "", "str", 0, True, "指数名称"),
    FieldSpec("value", "", "float", 4, True, "最新点位"),
    FieldSpec("change", "", "float", 4, False, "涨跌额"),
    FieldSpec("change_pct", "%", "float", 4, False, "涨跌幅，单位=%"),
]


# ---------------------------------------------------------------------------
# 单位换算基准
# ---------------------------------------------------------------------------
# 各数据源的「原始单位」声明，供 standardizer 换算到标准单位
SOURCE_UNITS = {
    "eastmoney": {"volume": "手", "amount": "元"},   # push2his / clist / ulist
    "tencent":   {"volume": "手", "amount": "万元"},  # qt.gtimg.cn：成交量(手)，成交额(万元)
    "sina":      {"volume": "股", "amount": "元"},    # hq.sinajs.cn：成交量(股)，成交额(元)
    "akshare":   {"volume": "手", "amount": "元"},    # stock_zh_a_hist：成交量(手)，成交额(元)
    "baostock":  {"volume": "股", "amount": "元"},    # query_history：volume(股)，amount(元)
    "ifind":     {"volume": "股", "amount": "元"},    # QuantAPI：成交量(股)，成交额(元)
}

# 单位换算系数：源单位 -> 标准单位
_VOLUME_TO_SHARES = {"股": 1.0, "手": 100.0}
_AMOUNT_TO_YUAN = {"元": 1.0, "千元": 1000.0, "万元": 10000.0, "亿元": 1e8}


def volume_to_shares(value: float, source_unit: str) -> float:
    """成交量 -> 股"""
    return value * _VOLUME_TO_SHARES.get(source_unit, 1.0)


def amount_to_yuan(value: float, source_unit: str) -> float:
    """成交额 -> 元"""
    return value * _AMOUNT_TO_YUAN.get(source_unit, 1.0)


# ---------------------------------------------------------------------------
# 代码格式统一
# ---------------------------------------------------------------------------
def symbols_digits(symbol: str) -> str:
    """取出代码中的纯数字部分"""
    if not symbol:
        return ""
    return "".join(ch for ch in str(symbol).split(".")[0] if ch.isdigit())


def is_beijing(symbol: str) -> bool:
    """判断是否为北交所标的。

    北交所号段：4xxxxx / 8xxxxx（老号段）+ 920xxx（新号段）。
    注意：沪B股 900xxx 仍属沪市，不能被误判。
    """
    code = symbols_digits(symbol)
    if not code:
        return False
    if code[:2] == "92":
        return True
    return code[:1] in ("4", "8")


def is_tradable_a_stock(symbol: str) -> bool:
    """是否为系统分析范围内的 A 股（沪深两市，不含北交所）。

    本项目定位沪深 A 股（沪 6xxxxx/5xxxxx 基金、深 0xxxxx/3xxxxx/1xxxxx 基金），
    北交所标的不纳入分析。
    """
    if not symbol:
        return False
    return not is_beijing(symbol)


def normalize_symbol(symbol: str, market_hint: str = None) -> str:
    """把任意写法的代码统一为 600519.SH 形式。

    支持输入: 600519 / 600519.SH / sh600519 / SH600519 / 1.600519
    """
    if not symbol:
        return symbol
    s = str(symbol).strip().upper()

    # 1.600519 / 0.000001（东财 secid）
    if "." in s and s.split(".")[0] in ("0", "1"):
        prefix, code = s.split(".", 1)
        exchange = "SH" if prefix == "1" else "SZ"
        return f"{code}.{exchange}"

    # sh600519 / sz000001 / bj920781 / SH600519
    if s[:2] in ("SH", "SZ", "BJ") and s[2:].isdigit():
        return f"{s[2:]}.{s[:2]}"

    # 600519.SH / 920781.BJ
    if s.endswith((".SH", ".SZ", ".BJ")):
        return s

    # 纯数字，按前缀推断
    code = "".join(ch for ch in s if ch.isdigit())
    if market_hint:
        return f"{code}.{market_hint.upper()}"
    # 北交所：4xxxxx / 8xxxxx / 920xxx（必须先于「9 -> 沪市」判断）
    if is_beijing(code):
        return f"{code}.BJ"
    # 沪市：6xxxxx(主板) / 5xxxxx(基金) / 9xxxxx(沪B，如900xxx)
    if code.startswith(("6", "5", "9")):
        return f"{code}.SH"
    return f"{code}.SZ"


def symbol_exchange(symbol: str) -> str:
    """从标准代码取交易所后缀"""
    return symbol.split(".")[-1] if "." in symbol else ""


# ---------------------------------------------------------------------------
# 标准字段清单（供校验/文档使用）
# ---------------------------------------------------------------------------
FIELD_REGISTRY = {
    "kline": KLINE_FIELDS,
    "quote": QUOTE_FIELDS,
    "stock_info": STOCK_INFO_FIELDS,
    "index": INDEX_FIELDS,
}


def field_names(kind: str):
    return [f.name for f in FIELD_REGISTRY[kind]]


def describe_standard() -> str:
    """生成数据标准的人类可读说明（供文档/接口返回）"""
    lines = ["# 数据标准（Standard Schema）", ""]
    for kind, specs in FIELD_REGISTRY.items():
        lines.append(f"## {kind}")
        lines.append("| 字段 | 单位 | 类型 | 精度 | 必填 | 说明 |")
        lines.append("|------|------|------|------|------|------|")
        for f in specs:
            lines.append(f"| {f.name} | {f.unit or '-'} | {f.dtype} | {f.precision} | "
                         f"{'是' if f.required else '否'} | {f.description} |")
        lines.append("")
    lines.append("## 单位换算基准")
    lines.append("| 数据源 | volume 原始单位 | amount 原始单位 |")
    lines.append("|--------|----------------|----------------|")
    for src, u in SOURCE_UNITS.items():
        lines.append(f"| {src} | {u['volume']} | {u['amount']} |")
    return "\n".join(lines)