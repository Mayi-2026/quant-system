"""因子有效性检验：在"三周期多头排列"候选池内，各因子与次日收益的 IC / 分组收益。

目的：找出真正有正向预测力的因子，据此重构打分模型；
若无因子有效，则如实告知用户该形态本身不具备次日预测力。
"""
import sqlite3
from collections import defaultdict
from pathlib import Path

import numpy as np
import pandas as pd

DB = Path(__file__).resolve().parent.parent / "data" / "quant.db"
OUT = Path(__file__).resolve().parent.parent / "outputs"

con = sqlite3.connect(str(DB), timeout=300)
df = pd.read_sql("SELECT symbol,trade_date,close,volume,amount FROM market_data WHERE close IS NOT NULL", con)
con.close()
df["trade_date"] = pd.to_datetime(df["trade_date"])
close = df.pivot_table(index="trade_date", columns="symbol", values="close").sort_index()
vol = df.pivot_table(index="trade_date", columns="symbol", values="volume").sort_index()
amt = df.pivot_table(index="trade_date", columns="symbol", values="amount").sort_index()
del df

ma5, ma10, ma20, ma60 = (close.rolling(x).mean() for x in (5, 10, 20, 60))
d_bull = (ma5 > ma10) & (ma10 > ma20) & (ma20 > ma60)


def bull_forward(rule, windows=(5, 10, 20), min_bars=20):
    r = close.resample(rule).last()
    m = [r.rolling(w).mean() for w in windows]
    b = (m[0] > m[1]) & (m[1] > m[2]) & (r.notna().cumsum() >= min_bars)
    return b.reindex(close.index, method="ffill")


w_bull = bull_forward("W-FRI")
m_bull = bull_forward("ME")

nxt = close.shift(-1) / close - 1
today = close / close.shift(1) - 1
v5, v20 = vol.rolling(5).mean(), vol.rolling(20).mean()
a20 = amt.rolling(20).mean()
vol20r = close.pct_change(fill_method=None).rolling(20).std() * np.sqrt(252) * 100
hi60, lo60 = close.rolling(60).max(), close.rolling(60).min()
cm60 = close.rolling(60).max()
mdd60 = (close / cm60 - 1) * 100
dd = close.diff()
up6 = dd.clip(lower=0).rolling(6).mean()
dn6 = (-dd.clip(upper=0)).rolling(6).mean()
rsi6 = (100 - 100 / (1 + up6 / dn6.replace(0, np.nan))).where(dn6 != 0, 100).where(up6 != 0, 0)

dates = [d for d in close.index if d >= pd.Timestamp("2022-06-01")][:-1]

# 累加每个因子的 (值, 次日收益) 对
pairs = defaultdict(list)
for T in dates:
    cm = (d_bull.loc[T].fillna(False)
          & w_bull.loc[T].reindex(close.columns).fillna(False)
          & m_bull.loc[T].reindex(close.columns).fillna(False))
    cand = close.columns[cm.values]
    if len(cand) < 10:
        continue
    c20 = close.loc[:T, cand].iloc[-21:].values
    c60 = close.loc[:T, cand].iloc[-60:].values
    if c20.shape[0] < 21 or c60.shape[0] < 60:
        continue
    last = c20[-1]
    y = c20[-20:]
    x = np.arange(20); xc = x - x.mean()
    ym = y.mean(axis=0); yc = y - ym
    slope = (xc[:, None] * yc).sum(axis=0) / (xc ** 2).sum()
    r2 = np.where((yc ** 2).sum(axis=0) > 0, 1 - ((yc - (slope * xc[:, None])) ** 2).sum(axis=0) / (yc ** 2).sum(axis=0), 0)
    nx = nxt.loc[T, cand].values

    fx = {
        "mom_1d": today.loc[T, cand].values,
        "mom_5d": last / c20[-6] - 1,
        "mom_20d": last / c20[0] - 1,
        "slope20": slope / np.where(ym != 0, ym, np.nan),
        "r2_20": r2,
        "bias20": last / ma20.loc[T, cand].values - 1,
        "vol_ratio": v5.loc[T, cand].values / v20.loc[T, cand].values,
        "log_amt20": np.log10(np.maximum(a20.loc[T, cand].values, 1)),
        "last_vol_ratio": vol.loc[T, cand].values / v20.loc[T, cand].values,
        "pos60": (last - lo60.loc[T, cand].values) / np.maximum(hi60.loc[T, cand].values - lo60.loc[T, cand].values, 1e-9),
        "vol20": vol20r.loc[T, cand].values,
        "mdd60": mdd60.loc[T, cand].values,
        "rsi6": rsi6.loc[T, cand].values,
        "price": last,
    }
    ok = np.isfinite(nx)
    for k, v in fx.items():
        v = np.asarray(v, dtype=float)
        m = ok & np.isfinite(v)
        for val, ret in zip(v[m], nx[m]):
            pairs[k].append((val, ret))

print(f"样本对数量（每因子）约 {len(pairs['mom_1d']):,}\n")
print(f"{'因子':<16}{'IC(全样本)':>12}{'Q1均收益':>11}{'Q5均收益':>11}{'Q5-Q1':>10}{'样本':>9}")
rows = []
for k, p in pairs.items():
    arr = np.array(p)
    vals, rets = arr[:, 0], arr[:, 1]
    # Spearman IC = 秩的 Pearson（不依赖 scipy）
    ic = np.corrcoef(pd.Series(vals).rank().values, pd.Series(rets).rank().values)[0, 1]
    q = pd.qcut(pd.Series(vals), 5, labels=False, duplicates="drop")
    q1 = rets[(q == 0).values].mean() * 100
    q5 = rets[(q == 4).values].mean() * 100
    rows.append((k, ic, q1, q5, q5 - q1, len(arr)))
for k, ic, q1, q5, diff, n in sorted(rows, key=lambda r: -abs(r[1])):
    print(f"{k:<16}{ic:>12.4f}{q1:>10.3f}%{q5:>10.3f}%{diff:>9.3f}%{n:>9,}")

print("\n注：IC>0 表示因子值越大次日收益越高；Q5-Q1>0 表示单调正向。")