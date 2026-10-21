"""校正某交易日的日线为「收盘价」（默认最新交易日）

背景：库里的当日日线可能是盘中快照（例如 2026-10-19 的行在 12:16 写入，
成交量只有全日的 63%，收盘价也与真实收盘不符）。日线表必须存收盘价，
否则选股/回测/涨跌统计全部被污染。

做法：
  1. 备份当日原有行到 outputs/backup_market_data_<date>.csv（可回滚）；
  2. 用新浪批量行情（hq.sinajs.cn，一次 200 只）取当日收盘；
  3. 只更新 close/volume/amount/pct_change，其余字段保留；
  4. 打印差异统计，差异超过阈值的标的单独列出。

用法：
    python fix_today_close.py                # 最新交易日
    python fix_today_close.py --date 2026-10-19
    python fix_today_close.py --dry-run      # 只看差异不写库
"""
from __future__ import annotations

import argparse
import sqlite3
from datetime import datetime
from pathlib import Path

import pandas as pd

from datasources.sina import SinaSource

BASE = Path(__file__).resolve().parent.parent
DB = BASE / "data" / "quant.db"
OUT = BASE / "outputs"
BATCH = 200


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--date", default=None)
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    con = sqlite3.connect(str(DB), timeout=180)
    trade_date = args.date or con.execute("SELECT MAX(trade_date) FROM market_data").fetchone()[0]

    old = pd.read_sql(
        "SELECT symbol, open, high, low, close, volume, amount, pct_change, created_at "
        "FROM market_data WHERE trade_date = ?", con, params=(trade_date,))
    if old.empty:
        print(f"{trade_date} 无数据")
        return

    OUT.mkdir(parents=True, exist_ok=True)
    bak = OUT / f"backup_market_data_{trade_date}.csv"
    old.to_csv(bak, index=False, encoding="utf-8-sig")
    print(f"[备份] {len(old)} 行 -> {bak}")

    src = SinaSource()
    symbols = old["symbol"].tolist()
    quotes: dict[str, dict] = {}
    for i in range(0, len(symbols), BATCH):
        chunk = symbols[i:i + BATCH]
        try:
            for row in src.fetch_quote(chunk):
                if row.get("price"):
                    quotes[row["symbol"]] = row
        except Exception as e:
            print(f"  批次 {i//BATCH + 1} 失败: {e}")
        print(f"  已取 {min(i + BATCH, len(symbols))}/{len(symbols)}", end="\r")
    print(f"\n[新浪] 拿到 {len(quotes)} 只")

    rows, diffs = [], []
    for _, r in old.iterrows():
        q = quotes.get(r["symbol"])
        if not q:
            continue
        close_new = float(q["price"])
        old_close = float(r["close"]) if r["close"] is not None else None
        vol_new = q.get("volume")
        amt_new = q.get("amount")
        pct_new = q.get("pct_change")
        if old_close is None:
            continue
        gap = abs(close_new / old_close - 1) * 100
        rows.append((round(close_new, 4), int(vol_new) if vol_new is not None else None,
                     round(float(amt_new), 2) if amt_new is not None else None,
                     round(float(pct_new), 4) if pct_new is not None else None,
                     r["symbol"]))
        if gap > 0.05:
            diffs.append({"symbol": r["symbol"], "old": old_close, "new": close_new,
                          "gap_pct": round(gap, 2)})

    print(f"[差异] {len(diffs)} 只收盘价偏差 > 0.05%，最大 {max((d['gap_pct'] for d in diffs), default=0)}%")

    if args.dry_run:
        print("dry-run，不写库")
        return

    from services.market_clock import final_flag
    flag = final_flag(trade_date)
    con.executemany(
        "UPDATE market_data SET close = ?, volume = COALESCE(?, volume), "
        "amount = COALESCE(?, amount), pct_change = COALESCE(?, pct_change), is_final = ? "
        "WHERE symbol = ? AND trade_date = ?",
        [(row[0], row[1], row[2], row[3], flag, row[4], trade_date) for row in rows])
    con.commit()
    print(f"[已更新] {trade_date} 共 {len(rows)} 行，is_final={flag}")

    if diffs:
        d = pd.DataFrame(diffs).sort_values("gap_pct", ascending=False)
        print(d.head(10).to_string(index=False))
        d.to_csv(OUT / f"close_diff_{trade_date}.csv", index=False, encoding="utf-8-sig")
    con.close()


if __name__ == "__main__":
    main()
