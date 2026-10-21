"""最终打分模型（基于已验证的反向因子）——2026-10-14

关键发现（历史 411 个交易日验证，IC=+0.078，t=8.99，各子样本同向）：
  在「日/周/月线三周期多头排列」候选池内，**越"不过热"的个股次日越好**；
  即：低量能扩张、低乖离、低近期动量、低波动 -> 次日相对收益更高。
  这是典型的"均值回复 + 低波动"效应，与直觉相反但统计上高度显著。

打分（0~100，越大越好）= 5 个已验证因子的等权 z-score 线性映射：
  1. 量能克制  -今日量比(vs20日)      权重 25%
  2. 量能趋势  -5日/20日量比          权重 20%
  3. 乖离收敛  -20日均线乖离率        权重 20%
  4. 动量温和  -10日动量              权重 20%
  5. 波动收敛  -20日年化波动率        权重 15%
"""
import json
import sqlite3
from pathlib import Path

import numpy as np

TODAY = "2026-10-14"
OUT = Path(__file__).resolve().parent.parent / "outputs"

feats = json.loads((OUT / f"tech_features_{TODAY}.json").read_text(encoding="utf-8"))
con = sqlite3.connect(str(Path(__file__).resolve().parent.parent / "data" / "quant.db"), timeout=120)
meta = {r[0]: {"name": r[1], "industry": r[2], "is_st": r[3]}
        for r in con.execute("SELECT symbol,name,industry,is_st FROM stock_info")}
con.close()

# 取因子原始值（方向：统一为"越大越好"，故对"越大越差"的因子取负）
RAW = {
    "ext_vol": -1.0,      # 今日量比：越低越好
    "vr5_20": -1.0,       # 5/20量比：越低越好
    "bias20": -1.0,       # 乖离率：越低越好
    "mom10": -1.0,        # 10日动量：越低越好
    "vol20": -1.0,        # 波动率：越低越好
}
W = {"ext_vol": 0.25, "vr5_20": 0.20, "bias20": 0.20, "mom10": 0.20, "vol20": 0.15}
FN = {"ext_vol": "last_vol_ratio", "vr5_20": "vol_ratio5_20",
      "bias20": "ma_bias20", "mom10": "r10", "vol20": "vol20"}


def zcol(vals):
    v = np.asarray(vals, dtype=float)
    ok = np.isfinite(v)
    v2 = v.copy()
    v2[~ok] = np.nanmedian(v[ok]) if ok.any() else 0
    sd = v2.std()
    return (v2 - v2.mean()) / sd if sd > 0 else np.zeros_like(v2)


rows = []
for r in feats:
    rows.append(r)

# 计算 z 与组合分
zmat = {}
for k, src in FN.items():
    zmat[k] = zcol([r[src] for r in rows])

combo = np.zeros(len(rows))
for k in RAW:
    combo += W[k] * (RAW[k] * zmat[k])

# 用秩百分位映射到 0~100（避免压缩到 100，展示更清晰）
order = np.argsort(np.argsort(combo))
score100 = order / (len(combo) - 1) * 100

out = []
for i, r in enumerate(rows):
    m = meta.get(r["symbol"], {})
    out.append({
        **r,
        "name": m.get("name"), "industry": m.get("industry"), "is_st": int(m.get("is_st") or 0),
        "score": round(float(score100[i]), 1),
        "z": {k: round(float(RAW[k] * zmat[k][i]), 3) for k in RAW},
    })

out.sort(key=lambda x: -x["score"])
(OUT / f"final_rank_{TODAY}.json").write_text(
    json.dumps(out, ensure_ascii=False, indent=1), encoding="utf-8")

print(f"{'排名':>3} {'代码':<11}{'名称':<10}{'行业':<10}{'收盘':>8}{'今涨%':>7}{'评分':>7}"
      f"{'量能克制':>9}{'量趋势':>8}{'乖离':>7}{'动量':>7}{'波动':>7}")
for i, r in enumerate(out[:15], 1):
    z = r["z"]
    print(f"{i:>3} {r['symbol']:<11}{(r['name'] or ''):<10}{(r['industry'] or ''):<10}"
          f"{r['close']:>8.2f}{(r['pct'] or 0):>7.2f}{r['score']:>7.1f}"
          f"{z['ext_vol']:>9.2f}{z['vr5_20']:>8.2f}{z['bias20']:>7.2f}{z['mom10']:>7.2f}{z['vol20']:>7.2f}")
print(f"\n共 {len(out)} 只 -> outputs/final_rank_{TODAY}.json")