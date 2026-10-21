"""数据库配置

SQLite 并发要点（修复 "database is locked"）：
- 默认的 rollback-journal（journal_mode=delete）下，任何写入事务提交时都需要
  EXCLUSIVE 锁，必须等待所有读事务释放 SHARED 锁。本项目 /api/market/* 等接口
  会对 market_data（近 600 万行）做全表扫描，读事务一旦偏长，后台回测线程在
  commit 时就会抛出 sqlite3.OperationalError: database is locked。
- 因此这里统一开启 WAL：读不阻塞写、写不阻塞读，提交只需 write 锁，不再需要
  EXCLUSIVE；再配 busy_timeout 兜住偶发争用。
"""
from sqlalchemy import create_engine, event
from sqlalchemy.ext.declarative import declarative_base
from sqlalchemy.orm import sessionmaker
from config import DATABASE_URL

# busy_timeout：遇到锁时最多等待 30s（毫秒）再报错，避免瞬间争用直接失败
_BUSY_TIMEOUT_MS = 30000

engine = create_engine(
    DATABASE_URL,
    connect_args={
        "check_same_thread": False,  # 后台线程 / BackgroundTasks 复用连接
        "timeout": _BUSY_TIMEOUT_MS / 1000,  # sqlite3 层的锁等待（秒）
    },
    echo=False,
)


@event.listens_for(engine, "connect")
def _set_sqlite_pragma(dbapi_conn, _record):
    """每个物理连接建立时统一设置 PRAGMA（WAL + 等待 + 完整性约束）。

    - journal_mode=WAL：读写并发不再互相阻塞（核心修复）
    - busy_timeout：锁争用时排队等待而非立即报错
    - synchronous=NORMAL：WAL 下的安全/性能平衡
    - foreign_keys=ON：保证外键约束生效
    """
    cursor = dbapi_conn.cursor()
    try:
        cursor.execute("PRAGMA journal_mode=WAL")
        cursor.execute(f"PRAGMA busy_timeout={_BUSY_TIMEOUT_MS}")
        cursor.execute("PRAGMA synchronous=NORMAL")
        cursor.execute("PRAGMA foreign_keys=ON")
    finally:
        cursor.close()


SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)

Base = declarative_base()


def get_db():
    """获取数据库会话"""
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


def retry_on_lock(fn, session, *, attempts: int = 5, base_delay: float = 0.5):
    """在 "database is locked" 时回滚并整体重跑 *fn*（指数退避）。

    *fn* 内部应自带 ``session.commit()``。使用整体重跑而非仅重试 commit，
    是因为 rollback 会丢弃会话中待提交的对象，必须重新构造。
    """
    import logging
    import time

    logger = logging.getLogger(__name__)
    delay = base_delay
    for i in range(attempts):
        try:
            return fn()
        except Exception as e:  # noqa: BLE001 — 仅对锁错误重试，其余原样抛出
            session.rollback()
            if "locked" not in str(e).lower() or i == attempts - 1:
                raise
            logger.warning("数据库被锁，%.1fs 后重试（%d/%d）：%s", delay, i + 1, attempts, e)
            time.sleep(delay)
            delay *= 2


def _ensure_columns():
    """旧库兼容：给已存在的表补列（SQLite 不支持 ADD COLUMN IF NOT EXISTS）"""
    from sqlalchemy import text
    alters = [
        ("strategies", "ai_generated", "INTEGER DEFAULT 0"),
        ("strategies", "vibe_run_id", "TEXT"),
        ("strategies", "strategy_code", "TEXT"),
        ("backtest_records", "stock_pool", "TEXT"),
        ("backtest_records", "report_path", "TEXT"),
        ("watchlist", "rating", "TEXT"),
        ("watchlist", "report", "TEXT"),
        ("watchlist", "rating_date", "TEXT"),
        # 行情终态标记：1=日终收盘价 0=盘中快照（详见 services/market_clock.py）
        ("market_data", "is_final", "INTEGER DEFAULT 1"),
    ]
    with engine.connect() as conn:
        for table, column, ddl in alters:
            try:
                conn.execute(text(f"ALTER TABLE {table} ADD COLUMN {column} {ddl}"))
                conn.commit()
            except Exception:
                # 列已存在则忽略
                pass
        # 按交易日聚合的索引（旧库补建；已存在则秒返回）
        for ddl in (
            "CREATE INDEX IF NOT EXISTS idx_trade_date ON market_data (trade_date)",
        ):
            try:
                conn.execute(text(ddl))
                conn.commit()
            except Exception:
                pass


def init_db():
    """初始化数据库表"""
    from models import position, trade, strategy, market_data, backtest, ai_chat, paper_trade, watchlist, fundamental_data
    Base.metadata.create_all(bind=engine)
    _ensure_columns()