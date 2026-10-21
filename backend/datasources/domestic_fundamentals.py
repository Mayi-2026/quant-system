"""国内基本面数据抓取 + 标准化落库

把 TradingAgents 分析 A 股所需、但国外源（yfinance/SEC EDGAR/FRED/StockTwits/Reddit）
拿不到或口径不符的数据，从国内源抓取并标准化写入 quant.db：

    fundamental           公司估值/股本/市值快照（腾讯实时行情 + 东财个股信息）
    financial_statement   财务三表（新浪财经，资产负债表/利润表/现金流量表）
    news_article          个股新闻（东财 stock_news_em）
    macro_data            宏观指标（CPI/PPI/PMI/GDP/M2/LPR）
    insider_transaction   股东增减持（东财 stock_ggcg_em）

口径统一（与 datasources/standards.py 一致）：
    symbol=600519.SH，金额=元，数量=股，比率=%，日期=YYYY-MM-DD

用法（在 backend 目录下运行）：
    python -m datasources.domestic_fundamentals fetch --symbols 600519.SH,000001.SZ
    python -m datasources.domestic_fundamentals fetch --all          # 全部已落库 A 股（慢）
    python -m datasources.domestic_fundamentals fetch-macro          # 仅宏观指标
"""
from __future__ import annotations

import logging
import re
import sys
import urllib.request
from datetime import date
from pathlib import Path

logger = logging.getLogger(__name__)

# 元数据列（新浪三表末尾的说明列，不属于报表项目，落库时剔除）
_STMT_META_COLS = {"报告日", "数据源", "是否审计", "公告日期", "币种", "类型", "更新日期"}

_STMT_TYPE_MAP = {
    "资产负债表": "balance_sheet",
    "利润表": "income_statement",
    "现金流量表": "cash_flow",
}

_PREFIX = {"SH": "sh", "SZ": "sz", "BJ": "bj"}


def _main():
    import argparse
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    p = argparse.ArgumentParser(description="国内基本面数据标准化落库")
    sub = p.add_subparsers(dest="cmd")

    fe = sub.add_parser("fetch", help="抓取并落库基本面/三表/新闻/增减持")
    fe.add_argument("--symbols", help="逗号分隔标准代码，如 600519.SH,000001.SZ；不传则 --all 需显式指定")
    fe.add_argument("--all", action="store_true", help="处理 quant.db 中全部已落库 A 股")

    sub.add_parser("fetch-macro", help="抓取并落库宏观指标（CPI/PPI/PMI/GDP/M2/LPR）")

    args = p.parse_args()
    if args.cmd == "fetch-macro":
        summary = fetch_and_store_macro()
        print(f"宏观落库完成: 新增/更新 {summary['inserted']} 行（{summary['indicators']} 个指标）")
    elif args.cmd == "fetch":
        symbols = _resolve_symbols(args)
        if not symbols:
            p.error("请用 --symbols 或 --all 指定标的")
        summary = fetch_and_store(symbols)
        print(
            f"落库完成: {len(symbols)} 只标的，基本面 {summary['fundamental']} 条，"
            f"三表 {summary['statement']} 行，新闻 {summary['news']} 条，增减持 {summary['insider']} 条"
        )
    else:
        p.print_help()
        sys.exit(1)


def _resolve_symbols(args) -> list[str]:
    if args.all:
        import datasources.tradingagents_bridge as bridge
        return bridge.list_symbols()
    if args.symbols:
        from datasources import standards as std
        return [std.normalize_symbol(s) for s in args.symbols.split(",") if s.strip()]
    return []


# ---------------------------------------------------------------------------
# 基础工具
# ---------------------------------------------------------------------------
def _ak():
    try:
        import akshare as ak
        return ak
    except ImportError as e:
        raise RuntimeError(f"akshare 未安装: {e}")


def _f(v):
    try:
        if v is None:
            return None
        return float(v)
    except (ValueError, TypeError):
        return None


def _norm_date(v):
    """任意日期 -> YYYY-MM-DD（None 就地返回 None）"""
    if v is None or v == "":
        return None
    s = str(v).strip()
    m = re.match(r"^(\d{4})[-/年]?(\d{1,2})[-/月]?(\d{1,2})?", s)
    if m:
        return f"{m.group(1)}-{int(m.group(2)):02d}-{int(m.group(3) or 1):02d}"
    return None


def _to_date(v):
    s = _norm_date(v)
    if not s:
        return None
    try:
        return date.fromisoformat(s)
    except ValueError:
        return None


def _parse_cn_num(v):
    """'12.6亿' / '3,200万' / '1256197800' -> float（归一：亿×1e8、万×1e4）"""
    if v is None:
        return None
    s = str(v).replace(",", "").strip()
    if s in ("", "-", "--", "None", "nan", "NaN"):
        return None
    mult = 1.0
    if "亿" in s:
        mult, s = 1e8, s.replace("亿", "")
    elif "万" in s:
        mult, s = 1e4, s.replace("万", "")
    try:
        return float(s) * mult
    except ValueError:
        return None


# ---------------------------------------------------------------------------
# 数据抓取
# ---------------------------------------------------------------------------
def _fetch_tencent_parts(symbol: str) -> list[str] | None:
    """腾讯实时行情分区字段（GBK 文本，以 ~ 分隔）"""
    code, exch = symbol.split(".")
    prefix = _PREFIX.get(exch, "sh")
    url = f"http://qt.gtimg.cn/q={prefix}{code}"
    try:
        raw = urllib.request.urlopen(url, timeout=10).read().decode("gbk", errors="ignore")
    except Exception as e:
        logger.warning("腾讯行情失败 %s: %s", symbol, e)
        return None
    if '="' not in raw:
        return None
    return raw.split('="', 1)[1].rsplit('"', 1)[0].split("~")


def _fetch_fundamental(symbol: str) -> dict | None:
    """合并腾讯（估值）+ 东财（股本/市值/行业/上市时间）得到基本面快照"""
    ak = _ak()
    code = symbol.split(".")[0]
    rec: dict = {
        "symbol": symbol,
        "name": None, "industry": None, "list_date": None,
        "total_shares": None, "float_shares": None,
        "total_market_cap": None, "float_market_cap": None,
        "pe_ttm": None, "pb": None, "turnover": None,
        "as_of_date": date.today(),
    }

    # 东财个股信息
    try:
        df = ak.stock_individual_info_em(symbol=code)
        info = dict(zip(df["item"], df["value"]))
        rec["name"] = info.get("股票简称") or info.get("名称")
        rec["industry"] = info.get("行业")
        rec["list_date"] = _to_date(info.get("上市时间"))
        rec["total_shares"] = _parse_cn_num(info.get("总股本"))
        rec["float_shares"] = _parse_cn_num(info.get("流通股"))
        rec["total_market_cap"] = _parse_cn_num(info.get("总市值"))
        rec["float_market_cap"] = _parse_cn_num(info.get("流通市值"))
    except Exception as e:
        logger.warning("东财个股信息失败 %s: %s", symbol, e)

    # 腾讯实时：名称 + PE/PB/换手率（亿元口径市值做兜底）
    parts = _fetch_tencent_parts(symbol)
    if parts and len(parts) > 46:
        try:
            rec["name"] = rec["name"] or parts[1]
            rec["pe_ttm"] = _f(parts[39])
            rec["pb"] = _f(parts[46])
            rec["turnover"] = _f(parts[38])
            if rec["total_market_cap"] is None:
                v = _f(parts[45])
                rec["total_market_cap"] = v * 1e8 if v is not None else None
            if rec["float_market_cap"] is None:
                v = _f(parts[44])
                rec["float_market_cap"] = v * 1e8 if v is not None else None
        except (IndexError, ValueError) as e:
            logger.warning("腾讯行情解析失败 %s: %s", symbol, e)

    # 完全没拿到任何有效字段则视为无数据
    meaningful = {k: v for k, v in rec.items() if k not in ("symbol", "as_of_date") and v is not None}
    return rec if meaningful else None


def _fetch_statements(symbol: str):
    """新浪三表 -> (statement_type, report_date, item, value) 迭代器"""
    ak = _ak()
    code = symbol.split(".")[0]
    prefix = _PREFIX.get(symbol.split(".")[1], "sh")
    stock = f"{prefix}{code}"
    for cn_name, stype in _STMT_TYPE_MAP.items():
        try:
            df = ak.stock_financial_report_sina(stock=stock, symbol=cn_name)
        except Exception as e:
            logger.warning("三表失败 %s/%s: %s", symbol, cn_name, e)
            continue
        if df is None or df.empty:
            continue
        item_cols = [c for c in df.columns if c not in _STMT_TYPE_MAP and c not in {"报告日"} and c not in _STMT_META_COLS]
        for _, row in df.iterrows():
            rd = _to_date(row.get("报告日"))
            if rd is None:
                continue
            for col in item_cols:
                v = _f(row.get(col))
                if v is None:
                    continue
                yield {"symbol": symbol, "statement_type": stype, "report_date": rd, "item": str(col), "value": v}


def _fetch_news(symbol: str):
    """东财个股新闻 -> 迭代器"""
    ak = _ak()
    code = symbol.split(".")[0]
    try:
        df = ak.stock_news_em(symbol=code)
    except Exception as e:
        logger.warning("个股新闻失败 %s: %s", symbol, e)
        return
    if df is None or df.empty:
        return
    for _, row in df.iterrows():
        yield {
            "symbol": symbol,
            "scope": "ticker",
            "title": str(row.get("新闻标题") or "")[:256],
            "content": str(row.get("新闻内容") or ""),
            "source": str(row.get("文章来源") or ""),
            "url": str(row.get("新闻链接") or ""),
            "publish_time": str(row.get("发布时间") or "")[:19],
        }


def _fetch_insider_all():
    """东财股东增减持（全市场一次拉取后按代码过滤）"""
    ak = _ak()
    try:
        df = ak.stock_ggcg_em(symbol="全部")
    except Exception as e:
        logger.warning("增减持失败: %s", e)
        return []
    if df is None or df.empty:
        return []
    out = []
    for _, r in df.iterrows():
        out.append({
            "code": str(r.get("代码") or ""),
            "holder_name": str(r.get("股东名称") or ""),
            "change_type": str(r.get("持股变动信息-增减") or ""),
            "change_volume": _parse_cn_num(r.get("持股变动信息-变动数量")),
            "change_ratio": _f(r.get("持股变动信息-占总股本比例")),
            "start_date": _to_date(r.get("变动开始日")),
            "end_date": _to_date(r.get("变动截止日")),
            "announce_date": _to_date(r.get("公告日")),
        })
    return out


# ---------------------------------------------------------------------------
# 宏观指标
# ---------------------------------------------------------------------------
def _col(df, *names):
    for n in names:
        if n in df.columns:
            return n
    return None


def _sort_key(text) -> str:
    """'2026年09月份' -> '202609'；'2026年第1-2季度' -> '2026Q1'"""
    s = str(text or "")
    digits = re.findall(r"\d+", s)
    if not digits:
        return ""
    if "季度" in s:
        return f"{digits[0]}Q{digits[1] if len(digits) > 1 else '1'}"
    y = digits[0]
    m = digits[1] if len(digits) > 1 else "01"
    return f"{y}{m.zfill(2)}"


def _macro_rows():
    """各宏观接口 -> 统一 dict 迭代器"""
    ak = _ak()
    # CPI（同比/环比 — macro_china_cpi 最新且口径完整；yearly/monthly 接口已停更）
    try:
        df = ak.macro_china_cpi()
        mcol = _col(df, "月份")
        yoy = _col(df, "全国-同比增长")
        mom = _col(df, "全国-环比增长")
        for _, r in df.iterrows():
            yield dict(indicator="cpi_yearly", date_text=str(r.get(mcol) or ""), sort_date=_sort_key(r.get(mcol)),
                       value=_f(r.get(yoy)), prev=None, forecast=None)
            yield dict(indicator="cpi_monthly", date_text=str(r.get(mcol) or ""), sort_date=_sort_key(r.get(mcol)),
                       value=_f(r.get(mom)), prev=None, forecast=None)
    except Exception as e:
        logger.warning("宏观 cpi 失败: %s", e)
    # PPI
    try:
        df = ak.macro_china_ppi()
        mcol, vcol, ycol = _col(df, "月份"), _col(df, "当月"), _col(df, "当月同比增长")
        for _, r in df.iterrows():
            yield dict(indicator="ppi", date_text=str(r.get(mcol) or ""), sort_date=_sort_key(r.get(mcol)),
                       value=_f(r.get(vcol)), forecast=_f(r.get(ycol)), prev=None)
    except Exception as e:
        logger.warning("宏观 ppi 失败: %s", e)
    # PMI
    try:
        df = ak.macro_china_pmi()
        mcol = _col(df, "月份", "日期")
        val_col = _col(df, "制造业-PMI", "制造业", "今值", "现值")
        for _, r in df.iterrows():
            yield dict(indicator="pmi", date_text=str(r.get(mcol) or ""), sort_date=_sort_key(r.get(mcol)),
                       value=_f(r.get(val_col)), prev=None, forecast=None)
    except Exception as e:
        logger.warning("宏观 pmi 失败: %s", e)
    # GDP
    try:
        df = ak.macro_china_gdp()
        qcol = _col(df, "季度", "季度时间")
        vcol = _col(df, "国内生产总值-绝对值", "国内生产总值", "绝对值")
        ycol = _col(df, "国内生产总值-同比增长", "同比增长")
        for _, r in df.iterrows():
            yield dict(indicator="gdp", date_text=str(r.get(qcol) or ""), sort_date=_sort_key(r.get(qcol)),
                       value=_f(r.get(vcol)), forecast=_f(r.get(ycol)), prev=None)
    except Exception as e:
        logger.warning("宏观 gdp 失败: %s", e)
    # 货币供应 M2
    try:
        df = ak.macro_china_money_supply()
        mcol = _col(df, "月份", "统计时间")
        vcol = _col(df, "货币和准货币(M2)-数量(亿元)", "货币和准货币(M2)-数量", "货币和准货币(M2)")
        ycol = _col(df, "货币和准货币(M2)-同比增长")
        for _, r in df.iterrows():
            yield dict(indicator="m2", date_text=str(r.get(mcol) or ""), sort_date=_sort_key(r.get(mcol)),
                       value=_f(r.get(vcol)), forecast=_f(r.get(ycol)), prev=None)
    except Exception as e:
        logger.warning("宏观 m2 失败: %s", e)
    # LPR
    try:
        df = ak.macro_china_lpr()
        dcol = _col(df, "TRADE_DATE", "日期")
        for _, r in df.iterrows():
            yield dict(indicator="lpr_1y", date_text=str(r.get(dcol) or ""), sort_date=_sort_key(r.get(dcol)),
                       value=_f(r.get("LPR1Y")), prev=None, forecast=None)
            yield dict(indicator="lpr_5y", date_text=str(r.get(dcol) or ""), sort_date=_sort_key(r.get(dcol)),
                       value=_f(r.get("LPR5Y")), prev=None, forecast=None)
    except Exception as e:
        logger.warning("宏观 lpr 失败: %s", e)


# ---------------------------------------------------------------------------
# 落库（删除旧行 -> 插入新行，保证幂等）
# ---------------------------------------------------------------------------
def fetch_and_store(symbols: list[str]) -> dict:
    """抓取并落库指定标的的基本面/三表/新闻 + 全市场增减持"""
    from database import init_db
    from database import SessionLocal
    from models import Fundamental, FinancialStatement, NewsArticle, InsiderTransaction

    init_db()
    session = SessionLocal()
    summary = {"fundamental": 0, "statement": 0, "news": 0, "insider": 0}
    try:
        std_symbols = {_std(s) for s in symbols}
        insider_rows = _fetch_insider_all()
        for s in symbols:
            # 基本面
            f = _fetch_fundamental(s)
            if f:
                session.query(Fundamental).filter(Fundamental.symbol == s).delete()
                session.add(Fundamental(**f))
                summary["fundamental"] += 1
            # 三表
            stmts = list(_fetch_statements(s))
            if stmts:
                session.query(FinancialStatement).filter(FinancialStatement.symbol == s).delete()
                for rec in stmts:
                    session.add(FinancialStatement(**rec))
                summary["statement"] += len(stmts)
            # 新闻
            news = list(_fetch_news(s))
            if news:
                session.query(NewsArticle).filter(NewsArticle.symbol == s).delete()
                for rec in news:
                    session.add(NewsArticle(**rec))
                summary["news"] += len(news)
            session.commit()
        # 增减持（全市场一次，按目标代码过滤后落库）
        from datasources import standards as std
        target_codes = {std.symbols_digits(s) for s in symbols}
        matched = [r for r in insider_rows if r["code"] in target_codes]
        if matched:
            session.query(InsiderTransaction).filter(
                InsiderTransaction.symbol.in_([std.normalize_symbol(r["code"]) for r in matched])
            ).delete()
            for r in matched:
                session.add(InsiderTransaction(
                    symbol=std.normalize_symbol(r["code"]),
                    holder_name=r["holder_name"] or None,
                    change_type=r["change_type"] or None,
                    change_volume=r["change_volume"],
                    change_ratio=r["change_ratio"],
                    start_date=r["start_date"],
                    end_date=r["end_date"],
                    announce_date=r["announce_date"],
                ))
            summary["insider"] += len(matched)
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()
    return summary


def _std(s: str) -> str:
    from datasources import standards as std
    return std.normalize_symbol(s)


def fetch_and_store_macro() -> dict:
    """抓取并落库宏观指标"""
    from database import init_db
    from database import SessionLocal
    from models import MacroData

    init_db()
    session = SessionLocal()
    inserted = 0
    indicators: set[str] = set()
    try:
        # 先清空旧宏观数据，保证重跑幂等（不累积重复行）
        session.query(MacroData).delete()
        for rec in _macro_rows():
            indicators.add(rec["indicator"])
            session.add(MacroData(
                indicator=rec["indicator"],
                date_text=rec["date_text"] or "",
                sort_date=rec["sort_date"] or None,
                value=rec["value"],
                prev=rec["prev"],
                forecast=rec["forecast"],
            ))
            inserted += 1
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()
    return {"inserted": inserted, "indicators": sorted(indicators)}


if __name__ == "__main__":
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
    _main()