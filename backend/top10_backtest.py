"""对最终 Top10 选股规则做历史验证（精确到"取组合分最高的10只"）。

输出：次日均收益、胜率、相对候选池/大盘超额、以及不同市场环境下的表现。
"""
import sqlite3
import warnings
from pathlib import Path

import numpy as np
import pandas as pd

warnings.filterwarnings("ignore")
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


def bull_fw(rule, windows=(5, 10, 20), min_bars=20):
    r = close.resample(rule).last()
    m = [r.rolling(w).mean() for w in windows]
    b = (m[0] > m[1]) & (m[1] > m[2]) & (r.notna().cumsum() >= min_bars)
    return b.reindex(close.index, method="ffill").infer_objects(copy=False)


w_bull, m_bull = bull_fw("W-FRI"), bull_fw("ME")
nxt = close.shift(-1) / close - 1
today = close / close.shift(1) - 1
v5, v20 = vol.rolling(5).mean(), vol.rolling(20).mean()
a20 = amt.rolling(20).mean()
vol20r = close.pct_change(fill_method=None).rolling(20).std() * np.sqrt(252) * 100
r10 = close / close.shift(10) - 1

W = {"ext_vol": 0.25, "vr5_20": 0.20, "bias20": 0.20, "mom10": 0.20, "vol20": 0.15}


def zf(v):
    v = np.asarray(v, float)
    ok = np.isfinite(v)
    if not ok.any():
        return None
    v = v.copy(); v[~ok] = np.nanmedian(v[ok])
    sd = v.std()
    return (v - v.mean()) / sd if sd > 0 else None


def combo_at(T, cand):
    parts = {
        "ext_vol": -(vol.loc[T, cand].values / v20.loc[T, cand].values),
        "vr5_20": -(v5.loc[T, cand].values / v20.loc[T, cand].values),
        "bias20": -(close.loc[T, cand].values / ma20.loc[T, cand].values - 1),
        "mom10": -r10.loc[T, cand].values,
        "vol20": -vol20r.loc[T, cand].values,
    }
    acc = np.zeros(len(cand)); used = 0
    for k, v in parts.items():
        z = zf(v)
        if z is not None:
            acc += W[k] * z; used += 1
    return acc / sum(W.values())


dates = [d for d in close.index if d >= pd.Timestamp("2022-06-01")][:-1]
recs = []
for T in dates:
    cm = (d_bull.loc[T].fillna(False) & w_bull.loc[T].reindex(close.columns).fillna(False)
          & m_bull.loc[T].reindex(close.columns).fillna(False))
    cand = close.columns[cm.values]
    # 剔除低流动性
    keep = a20.loc[T, cand].fillna(0).values >= 0.5e8
    cand = cand[keep]
    if len(cand) < 30:
        continue
    z = combo_at(T, cand)
    nx = nxt.loc[T, cand].values
    ok = np.isfinite(nx)
    if ok.sum() < 30:
        continue
    zz, nn = z[ok], nx[ok]
    order = np.argsort(-zz)
    top10 = nn[order[:10]]
    allc = nn
    mkt = np.nanmean(today.loc[T].values)  # 反向市场次日不合适；改用 nxt 全市场均值
    mkt_next = np.nanmean(nxt.loc[T].values)
    recs.append({
        "date": str(T.date()),
        "top10": top10.mean() * 100,
        "top10_win": (top10 > 0).mean() * 100,
        "pool": allc.mean() * 100,
        "mkt": mkt_next * 100,
        "excess_pool": (top10.mean() - allc.mean()) * 100,
        "excess_mkt": (top10.mean() - mkt_next) * 100,
    })

R = pd.DataFrame(recs)
R.to_json(OUT / "top10_backtest.json", orient="records", force_ascii=False, indent=1)
print(f"回测 {len(R)} 个交易日  ({R['date'].iloc[0]} ~ {R['date'].iloc[-1]})")
print(f"\n{'指标':<22}{'数值':>12}")
print(f"{'Top10 次日均收益':<22}{R['top10'].mean():>11.3f}%")
print(f"{'候选池次日均收益':<22}{R['pool'].mean():>11.3f}%")
print(f"{'全市场次日均收益':<22}{R['mkt'].mean():>11.3f}%")
print(f"{'Top10 相对候选池超额':<22}{R['excess_pool'].mean():>11.3f}%")
print(f"{'Top10 相对大盘超额':<22}{R['excess_mkt'].mean():>11.3f}%")
print(f"{'Top10 次日正收益胜率':<22}{R['top10_win'].mean():>11.1f}%")
print(f"{'Top10 跑赢候选池胜率':<22}{(R['top10']>R['pool']).mean()*100:>10.1f}%")
# 分年
R["year"] = R["date"].str[:4]
print(f"\n{'年份':<8}{'Top10':>10}{'候选池':>10}{'超额':>10}{'胜率':>8}")
for y, g in R.groupby("year"):
    print(f"{y:<8}{g['top10'].mean():>9.3f}%{g['pool'].mean():>9.3f}%{g['excess_pool'].mean():>9.3f}%{g['top10_win'].mean():>7.1f}%")