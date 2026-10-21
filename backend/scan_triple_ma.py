"""全市场日/周/月线多头排列扫描（2026-10-14 收盘后）

口径说明（重要）：
- 多头排列 = 短周期均线 > 中周期均线 > 长周期均线（自上而下依次排列）
  · 日线：MA5 > MA10 > MA20 > MA60
  · 周线：MA5 > MA10 > MA20
  · 月线：MA5 > MA10 > MA20
- 今日（2026-10-14）为周三，本周/本月 K 线尚未走完；
  与通达信等行情软件一致，当前周期的周/月 K 线用最新收盘实时计算。
- 数据源：本地 quant.db 日线历史（前复权/不复权混合）+ 今日腾讯收盘补齐。
- 样本：stock_info 中 SSE/SZSE 全部 A 股；剔除历史不足（月线<20根）与停牌无价标的。
"""
import json
import sqlite3
from pathlib import Path

import numpy as np
import pandas as pd

DB = Path(__file__).resolve().parent.parent / "data" / "quant.db"
TODAY = "2026-10-14"
OUT_DIR = Path(__file__).resolve().parent.parent / "outputs"

DAILY_MAS = [5, 10, 20, 60]
WK_MAS = [5, 10, 20]
MO_MAS = [5, 10, 20]
MIN_MONTH_BARS = 20


def load_prices():
    con = sqlite3.connect(str(DB), timeout=180)
    df = pd.read_sql(
        "SELECT symbol, trade_date, close FROM market_data WHERE close IS NOT NULL",
        con,
    )
    con.close()
    df["trade_date"] = pd.to_datetime(df["trade_date"])
    return df


def load_today():
    p = OUT_DIR / f"today_quotes_{TODAY}.json"
    return json.loads(p.read_text(encoding="utf-8"))


def load_names():
    con = sqlite3.connect(str(DB), timeout=120)
    rows = con.execute(
        "SELECT symbol, name, industry, is_st FROM stock_info WHERE market IN ('SSE','SZSE')"
    ).fetchall()
    con.close()
    return {r[0]: {"name": r[1], "industry": r[2], "is_st": r[3]} for r in rows}


def _aligned(ma: dict, order) -> bool:
    """均线自上而下多头排列，且全部为有效值。"""
    for k in order:
        if k not in ma or ma[k] is None or not np.isfinite(ma[k]):
            return False
    return all(ma[order[i]] > ma[order[i + 1]] for i in range(len(order) - 1))


def _mas(series: pd.Series, windows) -> dict:
    out = {}
    for w in windows:
        out[w] = float(series.rolling(w).mean().iloc[-1]) if len(series) >= w else None
    return out


def main():
    names = load_names()
    today = load_today()
    df = load_prices()
    # 用今日真实收盘覆盖/补齐 2026-10-14
    today_rows = pd.DataFrame(
        [{"symbol": s, "trade_date": pd.Timestamp(TODAY), "close": v["close"]}
         for s, v in today.items() if v.get("close")]
    )
    df = df[df["trade_date"] < pd.Timestamp(TODAY)]
    df = pd.concat([df, today_rows], ignore_index=True)

    results = []
    skipped_short = 0
    for sym, g in df.groupby("symbol", sort=False):
        g = g.sort_values("trade_date")
        closes = g.set_index("trade_date")["close"].astype(float)
        if len(closes) < 60:
            skipped_short += 1
            continue

        # 日线
        d_ma = _mas(closes, DAILY_MAS)
        if not _aligned(d_ma, DAILY_MAS):
            continue

        # 周线 / 月线：当前周期用最新收盘（未走完的 bar 实时更新）
        wk = closes.resample("W-FRI").last().dropna()
        mo = closes.resample("ME").last().dropna()
        if len(mo) < MIN_MONTH_BARS:
            skipped_short += 1
            continue
        w_ma = _mas(wk, WK_MAS)
        m_ma = _mas(mo, MO_MAS)
        if not _aligned(w_ma, WK_MAS):
            continue
        if not _aligned(m_ma, MO_MAS):
            continue

        info = names.get(sym, {})
        last = float(closes.iloc[-1])
        prev = float(closes.iloc[-2]) if len(closes) >= 2 else np.nan
        results.append({
            "symbol": sym,
            "name": info.get("name") or today.get(sym, {}).get("name") or "",
            "industry": info.get("industry") or "",
            "is_st": int(info.get("is_st") or 0),
            "close": last,
            "pct": round((last / prev - 1) * 100, 2) if np.isfinite(prev) and prev else None,
            "d_ma": d_ma, "w_ma": w_ma, "m_ma": m_ma,
            "daily_bars": len(closes), "month_bars": len(mo),
        })

    res = pd.DataFrame(results)
    print(f"输入A股(有价)={len(today)}  历史不足跳过={skipped_short}  "
          f"三周期同时多头排列={len(res)}")
    return res


if __name__ == "__main__":
    r = main()
    if len(r):
        r = r.sort_values(["is_st", "symbol"])
        r.to_json(OUT_DIR / f"triple_ma_bullish_{TODAY}.json",
                  orient="records", force_ascii=False, indent=1)
        print("已保存 outputs/triple_ma_bullish_%s.json" % TODAY)
        print(r[["symbol", "name", "industry", "close", "pct", "is_st"]].to_string(index=False))