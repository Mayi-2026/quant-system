"""Data Bridge：把 quant-system 的标准数据供给 vibe-trading

原理：vibe-trading 官方自带 ``local`` loader，读取
``~/.vibe-trading/data-bridge/config.yaml``，支持 csv / parquet / duckdb 三种载体。
本模块把 quant 标准化后的 K线（volume=股、amount=元）写成一个 DuckDB 文件，
并生成对应的 config.yaml，让 vibe 零改代码即可使用 quant 的数据。

同时负责把 config.SOURCE_PRIORITY 同步成 vibe 的 MARKET_DATA_ORDER_* 环境变量，
实现「改一处、两边生效」。

用法（在 backend 目录下）：
    python -m datasources.bridge export            # 导出全部持仓+自选标的
    python -m datasources.bridge export --symbols 600519.SH,000001.SZ
    python -m datasources.bridge sync-env          # 只同步数据源顺序
    python -m datasources.bridge status            # 查看桥接状态
"""
import logging
import os
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Optional

from . import config as ds_config
from . import standards as std
from .router import data_router

logger = logging.getLogger(__name__)

# 桥接产物目录
BRIDGE_DIR = Path.home() / ".vibe-trading" / "data-bridge"
BRIDGE_DB = BRIDGE_DIR / "quant_bridge.duckdb"
BRIDGE_CSV_DIR = BRIDGE_DIR / "csv"
BRIDGE_CONFIG = BRIDGE_DIR / "config.yaml"
VIBE_ENV = Path.home() / ".vibe-trading" / ".env"

# 导出用的默认历史长度（自然日）
DEFAULT_DAYS = 1825


def _duckdb_available() -> bool:
    try:
        import duckdb  # noqa: F401
        return True
    except ImportError:
        return False


# ---------------------------------------------------------------------------
# 导出：quant 标准K线 -> DuckDB（优先）或 CSV（零依赖回退）
# ---------------------------------------------------------------------------
def export_symbols(symbols: List[str], days: int = DEFAULT_DAYS,
                   carrier: str = "auto") -> Dict:
    """把指定标的的K线导出到桥接产物，并重建 vibe 的 data-bridge 配置。

    carrier: auto(有 duckdb 用之，否则 csv) / duckdb / csv
    返回 {"exported": n, "symbols": [...], "rows": {...}, "failed": [...], "carrier": ...}
    """
    from datetime import datetime as _dt, timedelta as _td

    if carrier == "auto":
        carrier = "duckdb" if _duckdb_available() else "csv"

    BRIDGE_DIR.mkdir(parents=True, exist_ok=True)
    start = (_dt.now() - _td(days=days)).strftime(std.STANDARD_DATE_FMT)
    end = _dt.now().strftime(std.STANDARD_DATE_FMT)

    # 1) 取数（无论哪种载体，都先取标准数据）
    fetched: Dict[str, list] = {}
    failed = []
    from services.market_clock import is_final_bar
    for sym in symbols:
        res = data_router.get_kline(sym, start, end, "daily", "qfq")
        data = res["data"]
        # 盘中抓到的当日 bar 不是收盘价，不能喂给 vibe 回测，直接裁掉
        data = [r for r in data if is_final_bar(r.get("trade_date"))]
        if not data:
            failed.append(sym)
            logger.warning(f"[bridge] 导出失败（无数据）: {sym}")
            continue
        fetched[sym] = data

    # 2) 落载体
    if carrier == "duckdb":
        _write_duckdb(fetched)
    else:
        _write_csv(fetched)

    # 3) 生成 vibe 配置
    _write_bridge_config(list(fetched.keys()), carrier)

    rows = {k: len(v) for k, v in fetched.items()}
    logger.info(f"[bridge] 导出完成({carrier}): {len(fetched)} 只, 失败 {len(failed)}")
    return {"exported": len(fetched), "symbols": list(fetched.keys()), "rows": rows,
            "failed": failed, "carrier": carrier,
            "generated_at": datetime.now().isoformat()}


def _write_duckdb(fetched: Dict[str, list]):
    import duckdb
    conn = duckdb.connect(str(BRIDGE_DB))
    conn.execute("""
        CREATE TABLE IF NOT EXISTS kline (
            symbol VARCHAR, trade_date DATE,
            open DOUBLE, high DOUBLE, low DOUBLE, close DOUBLE,
            volume DOUBLE, amount DOUBLE
        )
    """)
    for sym, data in fetched.items():
        conn.execute("DELETE FROM kline WHERE symbol = ?", [sym])
        conn.executemany(
            "INSERT INTO kline VALUES (?,?,?,?,?,?,?,?)",
            [(r["symbol"], r["trade_date"], r["open"], r["high"], r["low"],
              r["close"], r["volume"], r.get("amount")) for r in data],
        )
    conn.close()


def _write_csv(fetched: Dict[str, list]):
    import csv
    BRIDGE_CSV_DIR.mkdir(parents=True, exist_ok=True)
    for sym, data in fetched.items():
        path = BRIDGE_CSV_DIR / f"{sym.replace('.', '_')}.csv"
        with open(path, "w", newline="", encoding="utf-8") as f:
            w = csv.writer(f)
            w.writerow(["date", "open", "high", "low", "close", "volume", "amount"])
            for r in data:
                w.writerow([r["trade_date"], r["open"], r["high"], r["low"],
                            r["close"], r["volume"], r.get("amount")])


def export_watchlist_and_positions(carrier: str = "auto") -> Dict:
    """导出持仓 + 自选中的所有标的（日常同步的入口）"""
    from database import SessionLocal
    from models.position import Position
    from models.watchlist import Watchlist

    syms = set()
    db = SessionLocal()
    try:
        for p in db.query(Position).all():
            if p.symbol:
                syms.add(std.normalize_symbol(p.symbol))
        for w in db.query(Watchlist).all():
            if w.symbol:
                syms.add(std.normalize_symbol(w.symbol))
    finally:
        db.close()
    return export_symbols(sorted(syms), carrier=carrier)


# ---------------------------------------------------------------------------
# 生成 vibe 的 data-bridge config.yaml
# ---------------------------------------------------------------------------
def _write_bridge_config(symbols: List[str], carrier: str = "duckdb"):
    """为每个标的写一条 source 条目（duckdb 查询 或 csv 路径）。

    手写 YAML，不依赖 pyyaml（保证 CSV 载体零依赖可用）。
    """
    BRIDGE_DIR.mkdir(parents=True, exist_ok=True)
    lines = ["sources:"]
    for sym in symbols:
        lines.append(f"  - symbol: \"{sym}\"")
        if carrier == "duckdb":
            lines.append("    type: duckdb")
            lines.append(f"    db_path: \"{BRIDGE_DB}\"")
            query = (f"SELECT trade_date AS date, open, high, low, close, volume, amount "
                     f"FROM kline WHERE symbol = '{sym}' ORDER BY trade_date")
            lines.append(f"    query: \"{query}\"")
        else:
            path = BRIDGE_CSV_DIR / f"{sym.replace('.', '_')}.csv"
            lines.append("    type: csv")
            lines.append(f"    path: \"{path}\"")
    with open(BRIDGE_CONFIG, "w", encoding="utf-8") as f:
        f.write("\n".join(lines) + "\n")
    logger.info(f"[bridge] 已写 {BRIDGE_CONFIG} ({len(symbols)} 条, {carrier})")


# ---------------------------------------------------------------------------
# 同步数据源顺序到 vibe 的 env
# ---------------------------------------------------------------------------
def sync_source_order() -> Dict:
    """把 config.SOURCE_PRIORITY 同步成 vibe 的 MARKET_DATA_ORDER_* 环境变量。

    只更新/追加相关键，保留 .env 中其他配置。
    """
    order = ds_config.vibe_source_order("kline")
    var = "MARKET_DATA_ORDER_A_SHARE=" + ",".join(order)

    VIBE_ENV.parent.mkdir(parents=True, exist_ok=True)
    lines = []
    if VIBE_ENV.exists():
        lines = VIBE_ENV.read_text(encoding="utf-8").splitlines()

    key = "MARKET_DATA_ORDER_A_SHARE"
    replaced = False
    new_lines = []
    for line in lines:
        if line.strip().startswith(key + "="):
            new_lines.append(var)
            replaced = True
        else:
            new_lines.append(line)
    if not replaced:
        new_lines.append(var)

    VIBE_ENV.write_text("\n".join(new_lines).rstrip() + "\n", encoding="utf-8")
    logger.info(f"[bridge] 已同步数据源顺序到 {VIBE_ENV}: {order}")
    return {"env": str(VIBE_ENV), "var": var, "order": order, "replaced": replaced}


# ---------------------------------------------------------------------------
# 状态
# ---------------------------------------------------------------------------
def status() -> Dict:
    info = {
        "bridge_dir": str(BRIDGE_DIR),
        "duckdb_available": _duckdb_available(),
        "db_exists": BRIDGE_DB.exists(),
        "db_size": BRIDGE_DB.stat().st_size if BRIDGE_DB.exists() else 0,
        "csv_exists": BRIDGE_CSV_DIR.exists(),
        "config_exists": BRIDGE_CONFIG.exists(),
        "configured_symbols": 0,
        "vibe_env_exists": VIBE_ENV.exists(),
        "vibe_source_order": ds_config.vibe_source_order("kline"),
    }
    if BRIDGE_CONFIG.exists():
        try:
            text = BRIDGE_CONFIG.read_text(encoding="utf-8")
            info["configured_symbols"] = text.count("- symbol:")
            info["carrier"] = "duckdb" if "type: duckdb" in text else ("csv" if "type: csv" in text else None)
        except Exception:
            pass
    if BRIDGE_DB.exists():
        try:
            import duckdb
            conn = duckdb.connect(str(BRIDGE_DB), read_only=True)
            info["db_symbols"] = conn.execute(
                "SELECT COUNT(DISTINCT symbol) FROM kline").fetchone()[0]
            info["db_rows"] = conn.execute("SELECT COUNT(*) FROM kline").fetchone()[0]
            conn.close()
        except Exception as e:
            info["db_error"] = str(e)
    return info


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------
def _main():
    import argparse
    import sys
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    p = argparse.ArgumentParser(description="quant <-> vibe Data Bridge")
    sub = p.add_subparsers(dest="cmd")

    pe = sub.add_parser("export", help="导出K线到桥接产物并生成 vibe 配置")
    pe.add_argument("--symbols", help="逗号分隔的标的；不传则用持仓+自选")
    pe.add_argument("--days", type=int, default=DEFAULT_DAYS)
    pe.add_argument("--carrier", choices=["auto", "duckdb", "csv"], default="auto")

    sub.add_parser("sync-env", help="同步数据源顺序到 vibe .env")
    sub.add_parser("status", help="查看桥接状态")

    args = p.parse_args()
    if args.cmd == "export":
        if args.symbols:
            syms = [s.strip() for s in args.symbols.split(",") if s.strip()]
            r = export_symbols(syms, args.days, args.carrier)
        else:
            r = export_watchlist_and_positions(args.carrier)
        print(f"导出完成({r['carrier']}): {r['exported']} 只, 失败 {r['failed']}")
        for k, v in r.get("rows", {}).items():
            print(f"  {k}: {v} 行")
    elif args.cmd == "sync-env":
        r = sync_source_order()
        print(f"已同步: {r['var']}")
    elif args.cmd == "status":
        s = status()
        for k, v in s.items():
            print(f"  {k}: {v}")
    else:
        p.print_help()
        sys.exit(1)


if __name__ == "__main__":
    import sys
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
    _main()