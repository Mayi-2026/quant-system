"""market_data → Sequoia-X stock_daily 数据同步。

把量化系统 quant.db 的 market_data（前复权）按 Sequoia-X 的 stock_daily 表结构
同步一份到 sequoia_v2.db，供其 7 个选股策略消费。quant.db 每次更新行情后调用
一次增量同步即可，无需 Sequoia 自己回填。

字段映射：
    market_data.symbol      -> stock_daily.symbol   去 .SH/.SZ 后缀，保留纯数字
    market_data.trade_date  -> stock_daily.date     YYYY-MM-DD 文本
    market_data.amount(元)  -> stock_daily.turnover 成交额（Sequoia 内部即金额）
    open/high/low/close/volume 原样复制

注意：quant 的 turnover 字段是换手率(%)，不能映射到 sequoia 的 turnover（那指成交额）。
"""

from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta
from pathlib import Path

# 本文件位于 quant-system/backend/services/sequoia_sync.py
QUANT_ROOT = Path(__file__).resolve().parents[2]              # .../quant-system
SEQUOIA_ROOT = Path(__file__).resolve().parents[3] / "Sequoia-X"  # .../lianghua/Sequoia-X

QUANT_DB = QUANT_ROOT / "data" / "quant.db"
SEQUOIA_DB = SEQUOIA_ROOT / "data" / "sequoia_v2.db"

_CREATE_TABLE_SQL = """
CREATE TABLE IF NOT EXISTS stock_daily (
    id       INTEGER PRIMARY KEY AUTOINCREMENT,
    symbol   TEXT    NOT NULL,
    date     TEXT    NOT NULL,
    open     REAL,
    high     REAL,
    low      REAL,
    close    REAL,
    volume   REAL,
    turnover REAL,
    UNIQUE (symbol, date)
);
"""

_CREATE_INDEX_SQL = """
CREATE INDEX IF NOT EXISTS idx_symbol_date ON stock_daily (symbol, date);
"""

# 去掉 symbol 的 .SH/.SZ 后缀
_SYMBOL_EXPR = (
    "CASE WHEN instr(symbol, '.') > 0 "
    "THEN substr(symbol, 1, instr(symbol, '.') - 1) ELSE symbol END"
)


def _conn_sequoia() -> sqlite3.Connection:
    """打开（必要时创建）sequoia_v2.db，确保 stock_daily 表与索引存在。"""
    SEQUOIA_DB.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(SEQUOIA_DB), timeout=300)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    conn.execute(_CREATE_TABLE_SQL)
    conn.execute(_CREATE_INDEX_SQL)
    conn.commit()
    return conn


def _sync(since: str | None = None) -> dict:
    """核心 upsert：ATTACH quant.db，把 market_data 灌进 stock_daily。

    参数:
        since: 仅同步 trade_date >= since 的行（增量）；None 表示全量。
    """
    if not QUANT_DB.exists():
        raise FileNotFoundError(f"quant.db 不存在: {QUANT_DB}")

    where = "WHERE close IS NOT NULL"
    params: list[str] = []
    if since:
        where += " AND trade_date >= ?"
        params.append(since)

    sql = (
        "INSERT INTO stock_daily (symbol, date, open, high, low, close, volume, turnover) "
        f"SELECT {_SYMBOL_EXPR}, trade_date, open, high, low, close, volume, amount "
        f"FROM quant.market_data {where} "
        "ON CONFLICT(symbol, date) DO UPDATE SET "
        "open=excluded.open, high=excluded.high, low=excluded.low, "
        "close=excluded.close, volume=excluded.volume, turnover=excluded.turnover"
    )

    conn = _conn_sequoia()
    try:
        conn.execute("ATTACH DATABASE ? AS quant", (str(QUANT_DB),))
        cur = conn.execute(sql, params)
        affected = cur.rowcount
        cur.close()
        # 先提交写入事务以释放对 quant 附件的读锁，再 DETACH，否则会报 locked
        conn.commit()
        try:
            conn.execute("DETACH DATABASE quant")
            conn.commit()
        except sqlite3.OperationalError:
            pass  # 关闭连接时会自动 detach
        return {"synced": affected, "since": since or "all"}
    finally:
        conn.close()


def sync_all() -> dict:
    """全量同步：把 quant.db 全部历史行情灌进 sequoia_v2.db。"""
    return _sync(since=None)


def sync_recent(days: int = 30) -> dict:
    """增量同步最近 N 天（每日收盘后的同步钩子用）。"""
    if not QUANT_DB.exists():
        return {"synced": 0, "since": None}

    probe = sqlite3.connect(str(QUANT_DB))
    try:
        maxd = probe.execute("SELECT MAX(trade_date) FROM market_data").fetchone()[0]
    finally:
        probe.close()

    if not maxd:
        return {"synced": 0, "since": None}

    try:
        d = datetime.strptime(str(maxd)[:10], "%Y-%m-%d")
    except ValueError:
        d = datetime.now()
    since = (d - timedelta(days=days)).strftime("%Y-%m-%d")
    return _sync(since=since)


def status() -> dict:
    """两侧数据库的规模与日期范围（供菜单页展示）。"""
    def _sum(db_path: Path, table: str, date_col: str) -> dict:
        info = {"exists": db_path.exists(), "rows": 0, "min_date": None, "max_date": None}
        if not info["exists"]:
            return info
        conn = sqlite3.connect(str(db_path))
        try:
            info["rows"] = conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
            mn, mx = conn.execute(
                f"SELECT MIN({date_col}), MAX({date_col}) FROM {table}"
            ).fetchone()
            info["min_date"], info["max_date"] = str(mn), str(mx)
        finally:
            conn.close()
        return info

    quant = _sum(QUANT_DB, "market_data", "trade_date")
    sequoia = _sum(SEQUOIA_DB, "stock_daily", "date")

    if quant["exists"]:
        conn = sqlite3.connect(str(QUANT_DB))
        try:
            quant["symbols"] = conn.execute("SELECT COUNT(*) FROM stock_info").fetchone()[0]
        except sqlite3.OperationalError:
            quant["symbols"] = None
        finally:
            conn.close()

    if sequoia["exists"]:
        conn = sqlite3.connect(str(SEQUOIA_DB))
        try:
            sequoia["symbols"] = conn.execute(
                "SELECT COUNT(DISTINCT symbol) FROM stock_daily"
            ).fetchone()[0]
        finally:
            conn.close()

    return {"quant": quant, "sequoia": sequoia}