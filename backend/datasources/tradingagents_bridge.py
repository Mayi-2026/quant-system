"""TradingAgents 数据桥：把 quant-system 落库的标准 K 线导出到 TradingAgents 本地缓存。

原理
----
TradingAgents 的 ``load_ohlcv()`` 优先读取本地缓存 CSV（本离线部署把
``_cache_is_fresh()`` 改为恒 True，缓存即权威、不再联网），缓存文件名约定为
``{canonical}-YFin-data.csv``。``normalize_symbol`` 会把 ``600519.SH`` 规范成
``600519.SS``（Yahoo 写法），``.SZ`` 不变。

本模块把 quant.db 里已落库的标准化 K 线（东财/腾讯/新浪等经标准化的前复权价，
volume=股、amount=元）按 TradingAgents 期望的列（Date/Open/High/Low/Close/
Adj Close/Volume）写入缓存目录，从而让市场数据 / 技术指标 / 验证快照三条路径
零联网运行。

用法（在 backend 目录下）：
    python -m datasources.tradingagents_bridge export                 # 导出全部 A 股
    python -m datasources.tradingagents_bridge export --symbols 600519.SH,000001.SZ
    python -m datasources.tradingagents_bridge status                 # 查看缓存状态
"""
import csv
import logging
import os
import sqlite3
from pathlib import Path
from typing import List, Optional, Dict

logger = logging.getLogger(__name__)

# quant.db 路径：backend 的上一级目录（quant-system）/data/quant.db
QUANT_DB = Path(__file__).resolve().parent.parent.parent / "data" / "quant.db"

# TradingAgents 本地缓存目录（default_config.py：~/.tradingagents/cache）
DEFAULT_CACHE_DIR = Path.home() / ".tradingagents" / "cache"

# TradingAgents load_ohlcv 期望的列（Adj Close 可选，但为兼容 get_stock_data 一并写出）
OHLCV_COLUMNS = ["Date", "Open", "High", "Low", "Close", "Adj Close", "Volume"]


def canonical_symbol(symbol: str) -> str:
    """把 quant 库符号转换为 TradingAgents canonical（Yahoo）符号。

    仅 A 股需要此映射：上海 ``.SH`` -> Yahoo 的 ``.SS``，深圳 ``.SZ`` 保持不变。
    其它符号原样返回（与 TradingAgents normalize_symbol 对非 A 股的行为一致）。
    """
    sym = (symbol or "").strip().upper()
    if sym.endswith(".SH"):
        return sym[:-3] + ".SS"
    return sym


def _connect():
    if not QUANT_DB.exists():
        raise FileNotFoundError(f"quant.db 不存在: {QUANT_DB}")
    return sqlite3.connect(str(QUANT_DB))


def list_symbols() -> List[str]:
    """返回 quant.db 中已落库的全部 A 股符号（原始 .SH/.SZ 口径）。"""
    with _connect() as conn:
        rows = conn.execute(
            "SELECT DISTINCT symbol FROM market_data ORDER BY symbol"
        ).fetchall()
    return [r[0] for r in rows]


def export_to_tradingagents(
    symbols: Optional[List[str]] = None,
    cache_dir: Optional[str] = None,
) -> Dict:
    """把指定（或全部）标的的标准化 K 线写入 TradingAgents 缓存目录。

    symbols 省略时导出全部已落库 A 股。
    返回 {"exported": n, "files": n, "rows": {...}, "failed": [...], "cache_dir": ...}
    """
    cache_dir = Path(cache_dir) if cache_dir else DEFAULT_CACHE_DIR
    cache_dir.mkdir(parents=True, exist_ok=True)

    if symbols is None:
        symbols = list_symbols()
    else:
        # 统一大小写，去掉空白
        symbols = [s.strip().upper() for s in symbols if s and s.strip()]

    exported = 0
    rows_map: Dict[str, int] = {}
    failed: List[str] = []

    with _connect() as conn:
        for sym in symbols:
            canonical = canonical_symbol(sym)
            out_name = f"{canonical}-YFin-data.csv"
            out_path = cache_dir / out_name

            cur = conn.execute(
                """
                SELECT trade_date, open, high, low, close, volume
                FROM market_data
                WHERE symbol = ?
                  AND close IS NOT NULL
                ORDER BY trade_date ASC
                """,
                (sym,),
            )
            records = cur.fetchall()
            if not records:
                failed.append(sym)
                logger.warning(f"[ta-bridge] 无数据，跳过: {sym}")
                continue

            # 前复权价即调整后可比价，Adj Close 直接复用 Close。
            with open(out_path, "w", newline="", encoding="utf-8") as f:
                w = csv.writer(f)
                w.writerow(OHLCV_COLUMNS)
                for trade_date, o, h, l, c, v in records:
                    c = float(c)
                    w.writerow([
                        str(trade_date),
                        _num(o),
                        _num(h),
                        _num(l),
                        c,
                        c,
                        _num(v),
                    ])

            rows_map[canonical] = len(records)
            exported += 1

    logger.info(f"[ta-bridge] 导出完成: {exported} 只 -> {cache_dir}（失败 {len(failed)}）")
    return {
        "exported": exported,
        "files": exported,
        "rows": rows_map,
        "failed": failed,
        "cache_dir": str(cache_dir),
    }


def _num(v):
    """None/空 -> 0.0，否则 float（缓存 CSV 列需要数值）。"""
    try:
        return float(v)
    except (TypeError, ValueError):
        return 0.0


def status(cache_dir: Optional[str] = None) -> Dict:
    """查看缓存目录现状 + quant.db 覆盖情况。"""
    cache_dir = Path(cache_dir) if cache_dir else DEFAULT_CACHE_DIR
    info: Dict = {
        "quant_db": str(QUANT_DB),
        "quant_db_exists": QUANT_DB.exists(),
        "cache_dir": str(cache_dir),
        "cache_dir_exists": cache_dir.exists(),
        "cached_files": 0,
    }
    if QUANT_DB.exists():
        try:
            with _connect() as conn:
                info["symbols_in_db"] = conn.execute(
                    "SELECT COUNT(DISTINCT symbol) FROM market_data"
                ).fetchone()[0]
                info["rows_in_db"] = conn.execute(
                    "SELECT COUNT(*) FROM market_data"
                ).fetchone()[0]
                info["latest_date"] = conn.execute(
                    "SELECT MAX(trade_date) FROM market_data"
                ).fetchone()[0]
        except Exception as e:
            info["db_error"] = str(e)
    if cache_dir.exists():
        info["cached_files"] = sum(
            1 for f in os.listdir(cache_dir) if f.endswith("-YFin-data.csv")
        )
    return info


def _main():
    import argparse
    import sys
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    p = argparse.ArgumentParser(description="quant-system <-> TradingAgents Data Bridge")
    sub = p.add_subparsers(dest="cmd")

    pe = sub.add_parser("export", help="导出标准 K 线到 TradingAgents 缓存目录")
    pe.add_argument("--symbols", help="逗号分隔的标的；不传则导出全部已落库 A 股")
    pe.add_argument("--cache-dir", help="缓存目录（默认 ~/.tradingagents/cache）")

    sub.add_parser("status", help="查看桥接状态")

    args = p.parse_args()
    if args.cmd == "export":
        syms = [s.strip() for s in args.symbols.split(",") if s.strip()] if args.symbols else None
        r = export_to_tradingagents(syms, args.cache_dir)
        print(f"导出完成: {r['exported']} 只 -> {r['cache_dir']}（失败 {r['failed']}）")
    elif args.cmd == "status":
        for k, v in status().items():
            print(f"  {k}: {v}")
    else:
        p.print_help()
        sys.exit(1)


if __name__ == "__main__":
    import sys
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
    _main()