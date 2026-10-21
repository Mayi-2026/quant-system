"""补齐近五年历史数据（增量）

现状：库里 5224 只股票中 5211 只只有约 1 年数据（2024-06 起）。
本脚本把所有股票补到近 5 年（约 1215 个交易日）。

设计：
  - 增量：已有 5 年数据的标的跳过（断点续传）
  - 并发拉取（ThreadPoolExecutor）+ 串行写库（避免 SQLite 写锁）
  - 走数据源路由 + 标准化，入库口径统一（volume=股, amount=元）

用法：
  python backfill_5y.py                  # 全市场补齐
  python backfill_5y.py --symbols 600519.SH,000001.SZ
  python backfill_5y.py --workers 10
  python backfill_5y.py --only-missing   # 只补记录数明显不足的
"""
import argparse
import logging
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from database import SessionLocal                      # noqa: E402
from models.market_data import MarketData              # noqa: E402
from datasources.router import data_router             # noqa: E402
from datasources import standards as std               # noqa: E402

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s", datefmt="%H:%M:%S")
logger = logging.getLogger("backfill")

DAYS = 1825          # 5 年
TARGET_ROWS = 1150   # 5 年交易日约 1215，低于此视为需要补


def _get_symbols(only_missing: bool):
    db = SessionLocal()
    try:
        if only_missing:
            from sqlalchemy import func
            rows = (db.query(MarketData.symbol, func.count(MarketData.id))
                    .group_by(MarketData.symbol).all())
            return [s for s, c in rows if c < TARGET_ROWS]
        return [r[0] for r in db.query(MarketData.symbol).distinct().all()]
    finally:
        db.close()


def _fetch_one(symbol: str, start: str, end: str):
    """拉取单只标的的 5 年标准K线；返回 (symbol, rows, error)"""
    try:
        res = data_router.get_kline(symbol, start, end, "daily", "qfq")
        return symbol, res["data"], None
    except Exception as e:
        return symbol, None, str(e)[:120]


def _write_rows(db, symbol: str, rows) -> int:
    """写库（批量优化版）。

    关键优化：**一次性**查出该标的已存在的交易日集合，再批量插入缺失行，
    避免逐行 SELECT（原实现每只 1200+ 次查询，是主要瓶颈）。
    """
    from sqlalchemy import select
    import datetime as _dt

    # 1) 一次查询取已存在的日期集合
    existing_dates = {
        d for (d,) in db.execute(
            select(MarketData.trade_date).where(MarketData.symbol == symbol)
        ).all()
    }

    # 2) 过滤出缺失行
    to_insert = []
    for row in rows:
        td = _dt.datetime.strptime(row["trade_date"], std.STANDARD_DATE_FMT).date()
        if td in existing_dates:
            continue
        to_insert.append({
            "symbol": symbol, "trade_date": td,
            "open": row["open"], "high": row["high"], "low": row["low"], "close": row["close"],
            "volume": row["volume"], "amount": row.get("amount"),
            "turnover": row.get("turnover"), "pct_change": row.get("pct_change"),
        })

    # 3) 批量插入
    if to_insert:
        db.bulk_insert_mappings(MarketData, to_insert)
    return len(to_insert)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--symbols", help="逗号分隔；不传=全市场")
    ap.add_argument("--workers", type=int, default=10)
    ap.add_argument("--only-missing", action="store_true", help="只补记录不足的标的")
    args = ap.parse_args()

    end = datetime.now().strftime(std.STANDARD_DATE_FMT)
    start = (datetime.now() - timedelta(days=DAYS)).strftime(std.STANDARD_DATE_FMT)

    if args.symbols:
        symbols = [s.strip() for s in args.symbols.split(",") if s.strip()]
    else:
        symbols = sorted(_get_symbols(args.only_missing))

    logger.info(f"待补标的: {len(symbols)} 只, 区间 {start} ~ {end}, 并发 {args.workers}")
    t0 = time.time()
    success = failed = records = done = 0
    src_count = {}
    lock = threading.Lock()

    db = SessionLocal()   # 主线程串行写库
    try:
        with ThreadPoolExecutor(max_workers=args.workers) as ex:
            futures = {ex.submit(_fetch_one, s, start, end): s for s in symbols}
            for fut in as_completed(futures):
                symbol, rows, err = fut.result()
                done += 1
                if err or not rows:
                    failed += 1
                else:
                    try:
                        added = _write_rows(db, symbol, rows)
                        records += added
                        success += 1
                        # 每 20 只提交一次，减少事务开销（仍保证可见性）
                        if done % 20 == 0:
                            db.commit()
                    except Exception as e:
                        db.rollback()
                        failed += 1
                        logger.warning(f"写库失败 {symbol}: {str(e)[:80]}")
                if done % 100 == 0 or done == len(symbols):
                    db.commit()
                    el = time.time() - t0
                    speed = done / el if el else 0
                    eta = (len(symbols) - done) / speed if speed else 0
                    logger.info(f"[{done}/{len(symbols)}] 成功{success} 失败{failed} "
                                f"新增{records} 速度{speed:.1f}/s ETA{eta/60:.1f}min")
        db.commit()
    finally:
        db.close()

    el = time.time() - t0
    logger.info(f"完成: 成功{success} 失败{failed} 新增{records}行 耗时{el/60:.1f}分钟")
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    sys.exit(main())