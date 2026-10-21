"""标准化与校验层

职责：把「任意数据源」的原始记录，转换成本项目定义的「数据标准」(standards.py)。
这是保证"不论数据从哪来，入库口径一致"的唯一关卡。

转换内容：
  1. 单位换算：volume 手->股(×100)，amount 万元->元(×10000)
  2. 格式统一：日期 -> YYYY-MM-DD；代码 -> 600519.SH
  3. 精度统一：按 FieldSpec.precision 四舍五入
  4. 字段补全：缺 pct_change 时按 pre_close/close 计算
  5. 字段裁剪：只保留标准字段，丢弃源特有字段
  6. 合法性校验：价格>0、高>=低 等，脏数据标记/丢弃

用法：
    from datasources.standardizer import standardize_kline
    rows = standardize_kline(raw_rows, source_name="eastmoney")
"""
import logging
from datetime import date, datetime
from typing import Dict, List, Optional, Tuple

from . import standards as std

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# 基础工具
# ---------------------------------------------------------------------------
def normalize_date(value) -> Optional[str]:
    """任意日期写法 -> YYYY-MM-DD"""
    if value is None or value == "":
        return None
    if isinstance(value, (datetime, date)):
        return value.strftime(std.STANDARD_DATE_FMT)
    s = str(value).strip()
    # 20261013
    if len(s) == 8 and s.isdigit():
        return f"{s[:4]}-{s[4:6]}-{s[6:]}"
    # 2026/10/13
    if "/" in s:
        return s.replace("/", "-")
    # 2026-10-13 00:00:00
    if " " in s:
        s = s.split(" ")[0]
    return s


def _round(value, precision: int):
    if value is None:
        return None
    try:
        return round(float(value), precision)
    except (ValueError, TypeError):
        return None


def _check(spec: std.FieldSpec, value) -> Tuple[bool, str]:
    if spec.required and value is None:
        return False, f"{spec.name} 必填但为空"
    if value is not None and spec.validate and not spec.validate(value):
        return False, f"{spec.name}={value} 不合法"
    return True, ""


def _coerce_float(rec: Dict, spec_list: List[std.FieldSpec]):
    """把 float 型字段的字符串值转为 float（在单位换算与校验之前调用）"""
    for spec in spec_list:
        if spec.dtype == "float" and spec.name in rec and rec[spec.name] is not None:
            rec[spec.name] = _round(rec[spec.name], spec.precision)


# ---------------------------------------------------------------------------
# K线标准化
# ---------------------------------------------------------------------------
def standardize_kline(rows: List[Dict], source_name: str) -> Dict:
    """把原始K线记录标准化。

    返回: {"data": [...标准记录...], "dropped": n, "errors": [...]}
    """
    units = std.SOURCE_UNITS.get(source_name, {"volume": "手", "amount": "元"})
    v_unit, a_unit = units["volume"], units["amount"]
    specs = {f.name: f for f in std.KLINE_FIELDS}

    data, dropped, errors = [], 0, []
    for raw in rows:
        rec = _convert_common(raw, source_name)
        _coerce_float(rec, std.KLINE_FIELDS)   # 先转 float，再换算

        # 单位换算（核心）
        if rec.get("volume") is not None:
            rec["volume"] = std.volume_to_shares(rec["volume"], v_unit)
        if rec.get("amount") is not None:
            rec["amount"] = std.amount_to_yuan(rec["amount"], a_unit)

        # 补全涨跌幅
        if rec.get("pct_change") is None and rec.get("pre_close") and rec.get("close"):
            rec["pct_change"] = round((rec["close"] / rec["pre_close"] - 1) * 100, 4)
        # 补全振幅
        if rec.get("amplitude") is None and rec.get("high") and rec.get("low") and rec.get("pre_close"):
            rec["amplitude"] = round((rec["high"] - rec["low"]) / rec["pre_close"] * 100, 4)

        # 精度统一 + 校验
        ok, reason = _finalize(rec, specs, std.KLINE_FIELDS)
        if ok and not _kline_semantic_ok(rec):
            ok, reason = False, "OHLC 关系不合法"
        if ok:
            data.append(rec)
        else:
            dropped += 1
            errors.append(reason)

    if dropped:
        logger.warning(f"[standardizer] {source_name} K线丢弃 {dropped} 条: {errors[:3]}")
    return {"data": data, "dropped": dropped, "errors": errors}


def _kline_semantic_ok(rec: Dict) -> bool:
    """OHLC 语义校验：high>=max(open,close)>=min(open,close)>=low"""
    try:
        o, h, l, c = rec.get("open"), rec.get("high"), rec.get("low"), rec.get("close")
        if None in (o, h, l, c):
            return True  # 缺值交给 required 校验
        if h < l:
            return False
        if h < max(o, c) - 1e-6:
            return False
        if l > min(o, c) + 1e-6:
            return False
        return True
    except (TypeError, ValueError):
        return False


# ---------------------------------------------------------------------------
# 实时行情标准化
# ---------------------------------------------------------------------------
def standardize_quote(rows: List[Dict], source_name: str) -> Dict:
    units = std.SOURCE_UNITS.get(source_name, {"volume": "手", "amount": "元"})
    v_unit, a_unit = units["volume"], units["amount"]
    specs = {f.name: f for f in std.QUOTE_FIELDS}

    data, dropped, errors = [], 0, []
    for raw in rows:
        rec = _convert_common(raw, source_name)
        _coerce_float(rec, std.QUOTE_FIELDS)
        if rec.get("volume") is not None:
            rec["volume"] = std.volume_to_shares(rec["volume"], v_unit)
        if rec.get("amount") is not None:
            rec["amount"] = std.amount_to_yuan(rec["amount"], a_unit)
        if rec.get("pct_change") is None and rec.get("pre_close") and rec.get("price"):
            rec["pct_change"] = round((rec["price"] / rec["pre_close"] - 1) * 100, 4)

        ok, reason = _finalize(rec, specs, std.QUOTE_FIELDS)
        if ok:
            data.append(rec)
        else:
            dropped += 1
            errors.append(reason)
    return {"data": data, "dropped": dropped, "errors": errors}


# ---------------------------------------------------------------------------
# 股票信息 / 指数标准化
# ---------------------------------------------------------------------------
def standardize_stock_info(rows: List[Dict]) -> List[Dict]:
    specs = {f.name: f for f in std.STOCK_INFO_FIELDS}
    out = []
    for raw in rows:
        rec = _convert_common(raw, "stock_info")
        rec["list_date"] = normalize_date(rec.get("list_date"))
        if rec.get("is_st") is not None:
            rec["is_st"] = int(bool(rec["is_st"]))
        ok, _ = _finalize(rec, specs, std.STOCK_INFO_FIELDS, keep_unknown=False)
        if ok:
            out.append(rec)
    return out


def standardize_index(rows: List[Dict]) -> List[Dict]:
    specs = {f.name: f for f in std.INDEX_FIELDS}
    out = []
    for raw in rows:
        rec = _convert_common(raw, "index")
        ok, _ = _finalize(rec, specs, std.INDEX_FIELDS, keep_unknown=False)
        if ok:
            out.append(rec)
    return out


# ---------------------------------------------------------------------------
# 公共转换
# ---------------------------------------------------------------------------
_COMMON_ALIASES = {
    # 各源杂七杂八的字段名 -> 标准名
    "date": "trade_date", "day": "trade_date", "time": "trade_date",
    "code": "symbol", "ts_code": "symbol", "secid": "symbol",
    "prev_close": "pre_close", "preclose": "pre_close", "yclose": "pre_close",
    "last": "price", "lastPrice": "price", "current": "price", "now": "price",
    "pctChg": "pct_change", "pct_chg": "pct_change", "chg_pct": "pct_change",
    "turn": "turnover", "turnover_rate": "turnover",
    "pcfNcfTTM": "pcf_ttm", "peTTM": "pe", "pbMRQ": "pb", "psTTM": "ps",
}


def _convert_common(raw: Dict, source_name: str) -> Dict:
    """字段名归一 + 代码归一 + 日期归一（单位换算在此不做，由调用方处理）"""
    rec = {}
    for k, v in raw.items():
        key = _COMMON_ALIASES.get(k, k)
        rec[key] = v
    if rec.get("symbol"):
        rec["symbol"] = std.normalize_symbol(rec["symbol"])
    if rec.get("trade_date") is not None:
        rec["trade_date"] = normalize_date(rec["trade_date"])
    return rec


def _finalize(rec: Dict, specs: Dict[str, std.FieldSpec],
              spec_list: List[std.FieldSpec], keep_unknown: bool = False):
    """精度统一 + 字段裁剪（只留标准字段）+ 校验"""
    if not keep_unknown:
        allowed = {f.name for f in spec_list}
        rec = {k: v for k, v in rec.items() if k in allowed}
    # 精度
    for name, spec in specs.items():
        if name in rec and spec.dtype == "float":
            rec[name] = _round(rec[name], spec.precision)
    # 校验
    errors = []
    for spec in spec_list:
        ok, msg = _check(spec, rec.get(spec.name))
        if not ok:
            errors.append(msg)
    if errors:
        return False, "; ".join(errors)
    return True, ""