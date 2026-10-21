"""交易日历 / 行情"终态"判定

核心问题：日线表里 `trade_date=今天` 的行，可能是**盘中快照**（例如 12:16 抓的），
也可能是**日终收盘价**。两者混在一张表里，会让均线、选股、涨跌统计全部失真
（2026-10-19 就踩过：茅台记 1337.37，真实收盘 1324.97，成交量只有全日 63%）。

这里给出唯一判定口径：
    trade_date < 今天                → 终态（历史数据，写一次不再动）
    trade_date == 今天 且 now >= 收盘 → 终态
    trade_date == 今天 且 now <  收盘 → 盘中快照（可被后续覆盖）
    trade_date > 今天                → 非法（视为非终态）

收盘时间 = MARKET_CLOSE_HHMM（默认 15:00）+ MARKET_CLOSE_GRACE_MIN（默认 5 分钟）。
留 5 分钟是给收盘集合竞价与行情源落库留缓冲，避免 15:00:01 抓到未定格的价。

配套写库函数见 services/market_writer.py：
    终态行 → 不覆盖（幂等，避免重复刷库）；
    非终态行 → 每次更新都覆盖（这正是"如果不是收盘价就继续覆盖"的落点）。
"""
from __future__ import annotations

import os
from datetime import date, datetime, time as dtime
from typing import Union

DateLike = Union[str, date, datetime]

_CLOSE_HHMM = os.getenv("MARKET_CLOSE_HHMM", "15:00")
_GRACE_MIN = int(os.getenv("MARKET_CLOSE_GRACE_MIN", "5"))


def _parse_hhmm(s: str) -> dtime:
    try:
        h, m = s.split(":")
        return dtime(int(h), int(m))
    except Exception:
        return dtime(15, 0)


CLOSE_TIME = _parse_hhmm(_CLOSE_HHMM)
FINAL_AFTER = dtime((CLOSE_TIME.hour + (CLOSE_TIME.minute + _GRACE_MIN) // 60) % 24,
                    (CLOSE_TIME.minute + _GRACE_MIN) % 60)


def _as_date(value: DateLike) -> date:
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    return datetime.strptime(str(value)[:10], "%Y-%m-%d").date()


def is_final_bar(trade_date: DateLike, now: datetime | None = None) -> bool:
    """该交易日的数据是否已是「日终收盘价」这一终态。"""
    now = now or datetime.now()
    d = _as_date(trade_date)
    today = now.date()

    if d < today:
        return True
    if d > today:
        return False
    # 当天是周末/非交易日：正常不会产生当日行，若有也视作终态避免反复重写
    if today.weekday() >= 5:
        return True
    return now.time() >= FINAL_AFTER


def bar_status(trade_date: DateLike, now: datetime | None = None) -> str:
    """给前端/日志用的可读状态：final / provisional / future"""
    now = now or datetime.now()
    d = _as_date(trade_date)
    if d > now.date():
        return "future"
    return "final" if is_final_bar(d, now) else "provisional"


def final_flag(trade_date: DateLike, now: datetime | None = None) -> int:
    """写库用：1=终态，0=盘中快照"""
    return 1 if is_final_bar(trade_date, now) else 0


def describe(now: datetime | None = None) -> dict:
    """状态自述，供 /api/data/... 展示"""
    now = now or datetime.now()
    return {
        "now": now.strftime("%Y-%m-%d %H:%M:%S"),
        "today": now.date().isoformat(),
        "close_time": CLOSE_TIME.strftime("%H:%M"),
        "final_after": FINAL_AFTER.strftime("%H:%M"),
        "today_is_final": is_final_bar(now.date(), now),
        "status": bar_status(now.date(), now),
    }
