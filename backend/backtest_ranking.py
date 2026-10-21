"""打分模型历史回测（高效版）：检验"打分高 -> 次日涨幅高"是否真有统计优势。

要点：
- 预先把周线/月线多头排列状态"前推到每个交易日"，避免逐日重采样。
- 逐日对候选集打分，统计 Top10 / Top20 / 全部 / Bottom10 的次日收益。
"""
import json
import sqlite3
from pathlib import Path

import numpy as np
import pandas as pd

DB = Path(__file__).resolve().parent.parent / "data" / "quant.db"
OUT = Path(__file__).resolve().parent.parent / "outputs"


def main():
    con = sqlite3.connect(str(DB), timeout=300)
    df = pd.read_sql(
        "SELECT symbol,trade_date,close,volume,amount FROM market_data WHERE close IS NOT NULL",
        con)
    con.close()
    df["trade_date"] = pd.to_datetime(df["trade_date"])
    close = df.pivot_table(index="trade_date", columns="symbol", values="close").sort_index()
    vol = df.pivot_table(index="trade_date", columns="symbol", values="volume").sort_index()
    amt = df.pivot_table(index="trade_date", columns="symbol", values="amount").sort_index()
    del df

    # ---- 日线均线 ----
    ma5, ma10, ma20, ma60 = (close.rolling(x).mean() for x in (5, 10, 20, 60))
    d_bull_all = (ma5 > ma10) & (ma10 > ma20) & (ma20 > ma60)

    # ---- 周线多头：重采样 -> 计算 -> 前推到日频（用 ffill 到下周结束）----
    def bull_forward(resample_rule, windows=(5, 10, 20), min_bars=20):
        r = close.resample(resample_rule).last()
        mas = [r.rolling(w).mean() for w in windows]
        bull = (mas[0] > mas[1]) & (mas[1] > mas[2])
        counts = r.notna().cumsum()
        bull = bull & (counts >= min_bars)
        # 该周期 bar 的"可用日"= 该 bar 对应的最后一个交易日；用 ffill 前推到下一 bar
        return bull.reindex(close.index, method="ffill")

    w_bull_all = bull_forward("W-FRI")
    m_bull_all = bull_forward("ME")

    # ---- 次日收益 ----
    nxt = close.shift(-1) / close - 1
    today_ret = close / close.shift(1) - 1

    # 预计算滚动量能
    v5 = vol.rolling(5).mean()
    v20 = vol.rolling(20).mean()
    a20 = amt.rolling(20).mean()
    vol20r = close.pct_change().rolling(20).std() * np.sqrt(252) * 100
    hi60 = close.rolling(60).max()
    lo60 = close.rolling(60).min()
    cm60 = close.rolling(60).max()
    mdd60 = (close / cm60 - 1) * 100
    # RSI6
    dd = close.diff()
    up6 = dd.clip(lower=0).rolling(6).mean()
    dn6 = (-dd.clip(upper=0)).rolling(6).mean()
    rsi6 = 100 - 100 / (1 + up6 / dn6.replace(0, np.nan))
    rsi6 = rsi6.where(dn6 != 0, 100).where(up6 != 0, 0)

    def clip01(x, lo, hi):
        return max(0.0, min(1.0, (x - lo) / (hi - lo)))

    def score(r, mkt):
        e = r["pct"] - mkt
        s_excess = clip01(e, 0, 10)
        r20 = r["r20"]
        s_r20 = clip01(r20, -5, 40) if r20 <= 40 else max(0.2, 1 - (r20 - 40) / 160)
        s_r5 = clip01(r["r5"], -10, 15)
        RS = 25 * (0.5 * s_excess + 0.3 * s_r20 + 0.2 * s_r5)
        s_slope = clip01(r["slope20"], 0, 1.5)
        s_r2 = clip01(r["r2_20"], 0.3, 0.85)
        TQ = 20 * (0.45 * s_slope + 0.40 * s_r2 + 0.15 * 0.5)
        vr = r["vr"]
        s_vr = clip01(vr, 0.5, 1.0) * 0.7 if vr < 1 else (
            clip01(vr, 1, 2) * 0.5 + 0.5 if vr <= 2 else max(0.25, 1 - (vr - 2) / 3))
        s_amt = clip01(r["amt"], 0.3, 3.0)
        s_lvr = clip01(r["lvr"], 0.6, 2.5)
        VOL = 20 * (0.40 * s_vr + 0.30 * s_amt + 0.30 * s_lvr)
        p60 = r["pos60"]
        s_pos = clip01(p60, 50, 95) if p60 <= 95 else max(0.35, 1 - (p60 - 95) / 10)
        b = r["bias"]
        if b <= 15:
            s_bias = 1.0
        elif b <= 30:
            s_bias = 1 - (b - 15) / 15 * 0.6
        else:
            s_bias = max(0.1, 0.4 - (b - 30) / 50 * 0.4)
        FORM = 15 * (0.40 * s_pos + 0.40 * s_bias + 0.20 * 0.6)
        s_vol = clip01(90 - r["vol20"], 0, 70)
        s_mdd = clip01(-r["mdd"], 0, 25)
        ap = r["vol20"] / 16
        s_atr = (clip01(ap, 0.5, 8) * 0.6 + 0.4) if ap <= 8 else max(0.2, 1 - (ap - 8) / 8)
        heat = (1 if r["rsi6"] > 90 else 0)
        s_heat = 1 - 0.35 * heat
        RISK = 20 * (0.35 * s_vol + 0.30 * s_mdd + 0.20 * s_atr + 0.15 * s_heat)
        return RS + TQ + VOL + FORM + RISK

    dates = close.index
    test_dates = [d for d in dates if d >= pd.Timestamp("2022-06-01")][:-1]
    recs = []
    # 调整后收盘（用近20日回归斜率）
    for i, T in enumerate(test_dates):
        cand_mask = (d_bull_all.loc[T].fillna(False)
                     & w_bull_all.loc[T].fillna(False).reindex(close.columns).fillna(False)
                     & m_bull_all.loc[T].fillna(False).reindex(close.columns).fillna(False))
        cand = close.columns[cand_mask.values]
        if len(cand) < 5:
            continue
        c_all = close.loc[T, cand]
        c20 = close.loc[:T, cand].iloc[-21:].values  # 21 x k
        c60 = close.loc[:T, cand].iloc[-60:].values
        if c20.shape[0] < 21 or c60.shape[0] < 60:
            continue
        last = c20[-1]
        r5 = last / c20[-6] - 1
        r20 = last / c20[0] - 1
        # 线性回归斜率 / R²（向量化）
        x = np.arange(20)
        xm = x.mean()
        xc = x - xm
        y = c20[-20:]
        ym = y.mean(axis=0)
        yc = y - ym
        slope = (xc[:, None] * yc).sum(axis=0) / (xc ** 2).sum()
        yhat = ym + slope * xc[:, None]
        ss_res = ((yc - (yhat - ym)) ** 2).sum(axis=0)
        ss_tot = (yc ** 2).sum(axis=0)
        r2 = np.where(ss_tot > 0, 1 - ss_res / ss_tot, 0)
        slope_pct = slope / np.where(ym != 0, ym, np.nan) * 100

        ma20v = ma20.loc[T, cand].values
        bias = (last / ma20v - 1) * 100
        vr = (v5.loc[T, cand] / v20.loc[T, cand]).values
        amt20 = (a20.loc[T, cand] / 1e8).values
        lvr = (vol.loc[T, cand] / v20.loc[T, cand]).values
        v20r = vol20r.loc[T, cand].values
        mdd = mdd60.loc[T, cand].values
        pos60 = ((last - lo60.loc[T, cand]) /
                 (hi60.loc[T, cand] - lo60.loc[T, cand]) * 100).values
        rs6 = rsi6.loc[T, cand].values
        pct_today = today_ret.loc[T, cand].values * 100
        mkt = np.nanmean(today_ret.loc[T].values) * 100

        rows = []
        for j, s in enumerate(cand):
            try:
                r = {
                    "pct": float(pct_today[j]), "r5": float(r5[j] * 100),
                    "r20": float(r20[j] * 100), "slope20": float(slope_pct[j]),
                    "r2_20": float(r2[j]), "vr": float(vr[j]), "amt": float(amt20[j]),
                    "lvr": float(lvr[j]), "pos60": float(pos60[j]), "bias": float(bias[j]),
                    "vol20": float(v20r[j]), "mdd": float(mdd[j]), "rsi6": float(rs6[j]),
                }
                if not all(np.isfinite([r["r5"], r["r20"], r["slope20"], r["vr"], r["amt"]])):
                    continue
                nx = nxt.loc[T, s]
                if pd.isna(nx):
                    continue
                rows.append((s, score(r, mkt), float(nx)))
            except Exception:
                continue
        if len(rows) < 5:
            continue
        rows.sort(key=lambda t: -t[1])
        vals = [t[2] for t in rows]
        recs.append({
            "date": str(T.date()), "n": len(rows), "mkt_next": mkt,
            "top10": np.mean(vals[:10]) * 100, "top20": np.mean(vals[:20]) * 100,
            "all": np.mean(vals) * 100, "bottom10": np.mean(vals[-10:]) * 100,
        })
        if len(recs) % 100 == 0:
            print(f"  ...{len(recs)} 日 (至 {T.date()})", flush=True)

    R = pd.DataFrame(recs)
    R.to_json(OUT / "backtest_ranking.json", orient="records", force_ascii=False, indent=1)
    print(f"\n回测交易日数 {len(R)}  样本 {R['date'].iloc[0]} ~ {R['date'].iloc[-1]}")
    print(f"{'组合':<10}{'次日均收益':>11}{'跑赢大盘':>11}{'胜率':>8}")
    for col, name in [("top10", "Top10"), ("top20", "Top20"), ("all", "全部候选"), ("bottom10", "Bottom10")]:
        win = (R[col] > R["mkt_next"]).mean() * 100
        print(f"{name:<10}{R[col].mean():>10.3f}%{(R[col] - R['mkt_next']).mean():>10.3f}%{win:>7.1f}%")
    print(f"\nTop10 相对候选池超额 {(R['top10'] - R['all']).mean():.3f}%/日")
    print(f"Top10 - Bottom10 多空 {(R['top10'] - R['bottom10']).mean():.3f}%/日")
    return R


if __name__ == "__main__":
    main()