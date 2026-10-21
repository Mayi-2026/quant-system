"""稳健性检验：反向因子组合的有效性是否稳定（分样本 / 剔除低流动性 / 双向）。

结论将决定最终打分模型的方向与权重。
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


def bull_forward(rule, windows=(5, 10, 20), min_bars=20):
    r = close.resample(rule).last()
    m = [r.rolling(w).mean() for w in windows]
    b = (m[0] > m[1]) & (m[1] > m[2]) & (r.notna().cumsum() >= min_bars)
    return b.reindex(close.index, method="ffill").infer_objects(copy=False)


w_bull = bull_forward("W-FRI")
m_bull = bull_forward("ME")

nxt = close.shift(-1) / close - 1
today = close / close.shift(1) - 1
v5, v20 = vol.rolling(5).mean(), vol.rolling(20).mean()
a20 = amt.rolling(20).mean()
vol20r = close.pct_change(fill_method=None).rolling(20).std() * np.sqrt(252) * 100
r10 = close / close.shift(10) - 1
hi60 = close.rolling(60).max()
lo60 = close.rolling(60).min()

# 反向因子（经验证：低值 -> 次日更好）；方向统一为"越大越好" = 取负
FACTORS = {
    "ext_vol": lambda T, c: -(vol.loc[T, c].values / v20.loc[T, c].values),        # 今日放量程度（取负）
    "vr5_20": lambda T, c: -(v5.loc[T, c].values / v20.loc[T, c].values),          # 5/20量比（取负）
    "bias20": lambda T, c: -(close.loc[T, c].values / ma20.loc[T, c].values - 1),  # 乖离（取负）
    "mom10": lambda T, c: -r10.loc[T, c].values,                                   # 10日动量（取负）
    "vol20": lambda T, c: -vol20r.loc[T, c].values,                                # 波动率（取负）
}


def zscore(v):
    v = np.asarray(v, float)
    ok = np.isfinite(v)
    if ok.sum() == 0:
        return None
    v = v.copy()
    v[~ok] = np.nanmedian(v[ok])
    sd = v.std()
    return (v - v.mean()) / sd if sd > 0 else None


def run(dates, min_amt=0.0, label=""):
    ics, dq = [], {i: [] for i in range(10)}
    for T in dates:
        cm = (d_bull.loc[T].fillna(False)
              & w_bull.loc[T].reindex(close.columns).fillna(False)
              & m_bull.loc[T].reindex(close.columns).fillna(False))
        cand = close.columns[cm.values]
        if min_amt > 0:
            keep = a20.loc[T, cand].fillna(0).values >= min_amt * 1e8
            cand = cand[keep]
        if len(cand) < 30:
            continue
        zs = []
        for fn in FACTORS.values():
            z = zscore(fn(T, cand))
            if z is not None:
                zs.append(z)
        if not zs:
            continue
        z = np.mean(zs, axis=0)
        nx = nxt.loc[T, cand].values
        ok = np.isfinite(nx)
        if ok.sum() < 30:
            continue
        ics.append(np.corrcoef(pd.Series(z[ok]).rank().values, pd.Series(nx[ok]).rank().values)[0, 1])
        q = pd.qcut(pd.Series(z[ok]), 10, labels=False, duplicates="drop")
        nz = nx[ok]
        for i in range(10):
            m = (q == i).values
            if m.sum():
                dq[i].append(nz[m].mean() * 100)
    ics = np.array(ics)
    if len(ics) == 0:
        print(f"{label}: 无样本")
        return None
    d1 = np.mean(dq[0]); d10 = np.mean(dq[9])
    print(f"{label:<26} 日数{len(ics):>4}  IC={ics.mean():+.4f} t={ics.mean()/ics.std()*np.sqrt(len(ics)):>5.2f}  "
          f"D1={d1:+.3f}%  D10={d10:+.3f}%  多空={d10-d1:+.3f}%/日")
    return {"label": label, "n": len(ics), "ic": ics.mean(),
            "t": ics.mean() / ics.std() * np.sqrt(len(ics)),
            "d1": d1, "d10": d10, "spread": d10 - d1}


all_dates = [d for d in close.index if d >= pd.Timestamp("2022-06-01")][:-1]
print("=== 稳健性检验（反向组合分）===")
res = []
res.append(run(all_dates, 0.0, "全样本·全部候选"))
res.append(run(all_dates, 0.5, "全样本·剔除日均额<0.5亿"))
res.append(run(all_dates, 1.0, "全样本·剔除日均额<1亿"))
# 分样本
mid = pd.Timestamp("2025-01-01")
res.append(run([d for d in all_dates if d < mid], 0.0, "前半段(2023-2024)"))
res.append(run([d for d in all_dates if d >= mid], 0.0, "后半段(2025-2026)"))
res.append(run([d for d in all_dates if d < mid], 0.5, "前半段·额>0.5亿"))
res.append(run([d for d in all_dates if d >= mid], 0.5, "后半段·额>0.5亿"))

import json
json.dump([r for r in res if r], open(OUT / "robustness.json", "w"), ensure_ascii=False, indent=1)
print("\n已保存 outputs/robustness.json")