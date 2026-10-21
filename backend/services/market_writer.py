"""日线写入的统一入口（终态/快照 覆盖规则在这里收敛）

规则（与 services/market_clock.py 配套）：
    ┌ 库里没有            → 插入，is_final 按当前时间判定
    ├ 库里是非终态(0)      → **覆盖**（盘中快照 → 收盘价，或盘中 → 更新盘中）
    └ 库里是终态(1)        → 不覆盖价格；仅在金额/换手为空时做一次补全（幂等）

这样"数据更新"可以随便多跑几次：
    盘中每跑一次都把当日行刷新为最新快照；15:05 之后再跑一次，同一条记录
    被自动改写为收盘价并标记终态，此后不再被误覆盖。
"""
from __future__ import annotations

from datetime import date, datetime
from typing import Dict, Iterable, List

from models.market_data import MarketData
from services.market_clock import final_flag

# 允许写入的行情字段（不带 is_final / created_at）
PRICE_FIELDS = ("open", "high", "low", "close", "volume", "amount", "turnover", "pct_change")


def _as_date(v) -> date:
    if isinstance(v, datetime):
        return v.date()
    if isinstance(v, date):
        return v
    return datetime.strptime(str(v)[:10], "%Y-%m-%d").date()


def upsert_daily_bars(db, symbol: str, rows: Iterable[Dict],
                      now: datetime | None = None, commit: bool = False) -> Dict[str, int]:
    """批量写入某只股票的日线，按终态规则决定插入/覆盖/跳过。

    返回 {"added", "updated", "patched", "skipped"}。
    性能：整个 symbol 只做 **1 次** SELECT（一次性取回区间内已有行），
    避免逐行 filter().first() —— 后者在 600 万行表上会让导入速度掉 50 倍。

    rows: [{"trade_date": "2026-10-19", "open": .., "close": .., ...}, ...]
    """
    stats = {"added": 0, "updated": 0, "patched": 0, "skipped": 0}

    # 同一批里同一天只保留最后一条
    by_date: Dict[date, Dict] = {}
    for row in rows:
        if not row or row.get("trade_date") is None:
            continue
        by_date[_as_date(row["trade_date"])] = row
    if not by_date:
        return stats

    d_min, d_max = min(by_date), max(by_date)
    existing = {
        r.trade_date: r
        for r in db.query(MarketData)
        .filter(MarketData.symbol == symbol,
                MarketData.trade_date >= d_min,
                MarketData.trade_date <= d_max)
        .all()
    }

    for d, row in sorted(by_date.items()):
        flag = final_flag(d, now)
        cur = existing.get(d)

        if cur is None:
            db.add(MarketData(
                symbol=symbol, trade_date=d,
                **{f: row.get(f) for f in PRICE_FIELDS},
                is_final=flag,
            ))
            stats["added"] += 1
            continue

        # 已是终态：价格一律不动，只补空白的金额/换手
        if int(cur.is_final or 0) == 1:
            patched = False
            for f in ("amount", "turnover"):
                if getattr(cur, f) in (None, 0) and row.get(f):
                    setattr(cur, f, row[f])
                    patched = True
            stats["patched" if patched else "skipped"] += 1
            continue

        # 非终态：覆盖（这正是"不是收盘价就继续覆盖更新"）
        changed = False
        for f in PRICE_FIELDS:
            val = row.get(f)
            if val is None:
                continue
            if getattr(cur, f) != val:
                setattr(cur, f, val)
                changed = True
        cur.is_final = flag
        stats["updated" if changed else "skipped"] += 1

    if commit:
        db.commit()
    return stats


def provisional_dates(db, limit_days: int = 5) -> List[dict]:
    """列出仍处于「非终态」的交易日（供前端提示"当日数据未定格"）。

    只扫最近的日期区间（配合 idx_trade_date，毫秒级）；无索引时全表 group by 要 20s+。
    """
    from datetime import date, timedelta
    from sqlalchemy import func

    since = date.today() - timedelta(days=limit_days * 4 + 7)
    rows = (
        db.query(MarketData.trade_date,
                 func.count(MarketData.id),
                 func.sum(MarketData.is_final))
        .filter(MarketData.trade_date >= since)
        .group_by(MarketData.trade_date)
        .order_by(MarketData.trade_date.desc())
        .limit(limit_days)
        .all()
    )
    out = []
    for d, total, finals in rows:
        finals = int(finals or 0)
        if finals < int(total or 0):
            out.append({"trade_date": str(d), "total": int(total or 0),
                        "final": finals, "provisional": int(total or 0) - finals})
    return out
