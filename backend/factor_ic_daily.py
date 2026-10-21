"""日频横截面 IC 分析 + 组合打分验证（严谨版）。

- 每日在候选池内对因子做横截面排序，计算与次日收益的 Spearman IC。
- 汇报 IC 均值、IC_IR(=mean/std)、t 值、IC>0 占比，判断因子稳定性。
- 用"按经验方向"构造的组合分做十分位回测，验证是否真的有超额。
"""
import sqlite3
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
dd = close.diff()
up6 = dd.clip(lower=0).rolling(6).mean()
dn6 = (-dd.clip(upper=0)).rolling(6).mean()
rsi6 = (100 - 100 / (1 + up6 / dn6.replace(0, np.nan))).where(dn6 != 0, 100).where(up6 != 0, 0)
r10 = close / close.shift(10) - 1

dates = [d for d in close.index if d >= pd.Timestamp("2022-06-01")][:-1]

ic_series = []
per_day = []
for T in dates:
    cm = (d_bull.loc[T].fillna(False)
          & w_bull.loc[T].reindex(close.columns).fillna(False)
          & m_bull.loc[T].reindex(close.columns).fillna(False))
    cand = close.columns[cm.values]
    if len(cand) < 20:
        continue
    c20 = close.loc[:T, cand].iloc[-21:].values
    if c20.shape[0] < 21:
        continue
    last = c20[-1]
    y = c20[-20:]
    x = np.arange(20); xc = x - x.mean()
    yc = y - y.mean(axis=0)
    slope = (xc[:, None] * yc).sum(axis=0) / (xc ** 2).sum()
    yhat = slope * xc[:, None]
    sst = (yc ** 2).sum(axis=0)
    r2 = np.where(sst > 0, 1 - ((yc - yhat) ** 2).sum(axis=0) / sst, 0)
    nx = nxt.loc[T, cand].values
    fx = {
        "mom_1d": today.loc[T, cand].values,
        "mom_5d": last / c20[-6] - 1,
        "mom_10d": r10.loc[T, cand].values,
        "mom_20d": last / c20[0] - 1,
        "slope20": slope / np.where(y.mean(axis=0) != 0, y.mean(axis=0), np.nan),
        "bias20": last / ma20.loc[T, cand].values - 1,
        "vol20": vol20r.loc[T, cand].values,
        "last_vol_ratio": (vol.loc[T, cand] / v20.loc[T, cand]).values,
        "vol_ratio5_20": (v5.loc[T, cand] / v20.loc[T, cand]).values,
        "log_amt20": np.log10(np.maximum(a20.loc[T, cand].values, 1)),
        "pos60": ((last - lo60.loc[T, cand].values) /
                  np.maximum(hi60.loc[T, cand].values - lo60.loc[T, cand].values, 1e-9)),
        "rsi6": rsi6.loc[T, cand].values,
    }
    ok = np.isfinite(nx)
    day = {"date": T, "n": int(ok.sum())}
    for k, v in fx.items():
        v = np.asarray(v, float)
        m = ok & np.isfinite(v)
        if m.sum() < 15:
            continue
        # 横截面 Spearman IC（秩 Pearson）
        rv = pd.Series(v[m]).rank().values
        rr = pd.Series(nx[m]).rank().values
        d_ic = np.corrcoef(rv, rr)[0, 1]
        day[k] = d_ic
    per_day.append(day)

P = pd.DataFrame(per_day).set_index("date")
factors = [c for c in P.columns if c != "n"]
print(f"回测日数 {len(P)}  平均候选池 {P['n'].mean():.0f} 只\n")
print(f"{'因子':<16}{'IC均值':>9}{'IC_IR':>9}{'t值':>8}{'IC>0占比':>10}")
stats = []
for k in factors:
    s = P[k].dropna()
    icm, ics = s.mean(), s.std()
    ir = icm / ics if ics else 0
    t = icm / ics * np.sqrt(len(s)) if ics else 0
    stats.append((k, icm, ir, t, (s > 0).mean() * 100))
for k, icm, ir, t, pos in sorted(stats, key=lambda r: -abs(r[3])):
    print(f"{k:<16}{icm:>9.4f}{ir:>9.3f}{t:>8.2f}{pos:>9.1f}%")

# ---- 组合分：按 IC 方向（负 IC -> 取负号）等权 z-score ----
print("\n=== 按经验方向的组合分（负IC因子取反）十分位次日收益 ===")
# 方向：IC 为负的因子 -> 取反（即"低值更好"）
directions = {k: (-1 if icm < 0 else 1) for k, icm, _, _, _ in stats}
print("方向:", {k: ("低→好" if v < 0 else "高→好") for k, v in directions.items()})

deciles = {i: [] for i in range(10)}
comb_ic = []
for T in dates:
    cm = (d_bull.loc[T].fillna(False)
          & w_bull.loc[T].reindex(close.columns).fillna(False)
          & m_bull.loc[T].reindex(close.columns).fillna(False))
    cand = close.columns[cm.values]
    if len(cand) < 30:
        continue
    c20 = close.loc[:T, cand].iloc[-21:].values
    if c20.shape[0] < 21:
        continue
    last = c20[-1]
    y = c20[-20:]
    x = np.arange(20); xc = x - x.mean()
    yc = y - y.mean(axis=0)
    slope = (xc[:, None] * yc).sum(axis=0) / (xc ** 2).sum()
    ymean = y.mean(axis=0)
    nx = nxt.loc[T, cand].values
    fx = {
        "mom_1d": today.loc[T, cand].values,
        "mom_5d": last / c20[-6] - 1,
        "mom_10d": r10.loc[T, cand].values,
        "mom_20d": last / c20[0] - 1,
        "slope20": slope / np.where(ymean != 0, ymean, np.nan),
        "bias20": last / ma20.loc[T, cand].values - 1,
        "vol20": vol20r.loc[T, cand].values,
        "last_vol_ratio": (vol.loc[T, cand] / v20.loc[T, cand]).values,
        "vol_ratio5_20": (v5.loc[T, cand] / v20.loc[T, cand]).values,
        "log_amt20": np.log10(np.maximum(a20.loc[T, cand].values, 1)),
        "pos60": ((last - lo60.loc[T, cand].values) /
                  np.maximum(hi60.loc[T, cand].values - lo60.loc[T, cand].values, 1e-9)),
        "rsi6": rsi6.loc[T, cand].values,
    }
    # 等权 z-score（按方向）
    z = np.zeros(len(cand))
    cnt = 0
    for k, v in fx.items():
        v = np.asarray(v, float)
        if not np.isfinite(v).all():
            v = np.nan_to_num(v, nan=np.nanmedian(v[np.isfinite(v)]))
        sd = v.std()
        if sd == 0:
            continue
        z += directions[k] * (v - v.mean()) / sd
        cnt += 1
    if cnt == 0:
        continue
    z /= cnt
    ok = np.isfinite(nx)
    if ok.sum() < 30:
        continue
    d_ic = np.corrcoef(pd.Series(z[ok]).rank().values, pd.Series(nx[ok]).rank().values)[0, 1]
    comb_ic.append(d_ic)
    q = pd.qcut(pd.Series(z[ok]), 10, labels=False, duplicates="drop")
    nz = nx[ok]
    for i in range(10):
        m = (q == i).values
        if m.sum():
            deciles[i].append(nz[m].mean() * 100)

comb_ic = np.array(comb_ic)
print(f"\n组合分 IC均值={comb_ic.mean():.4f}  IC_IR={comb_ic.mean()/comb_ic.std():.3f}  "
      f"t={comb_ic.mean()/comb_ic.std()*np.sqrt(len(comb_ic)):.2f}  IC>0占比={(comb_ic>0).mean()*100:.1f}%")
print(f"\n{'十分位':<8}{'次日均收益':>12}{'样本日数':>10}")
for i in range(10):
    if deciles[i]:
        print(f"D{i+1:<7}{np.mean(deciles[i]):>11.3f}%{len(deciles[i]):>10}")
d1 = np.mean(deciles[0]); d10 = np.mean(deciles[9])
print(f"\nD1-D10 多空差: {d10 - d1:.3f}%/日   (D10 为组合分最高组)")
np.save(OUT / "comb_ic.npy", comb_ic)