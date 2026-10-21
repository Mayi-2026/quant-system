"""73 只三周期多头排列个股 —— 多因子综合打分与 Top10 排序（2026-10-14）

打分维度（总分 100，权重透明可调）：
  1. 相对强度 RS  (25)  —— 个股动量 相对 当日市场（-1.77%）
  2. 趋势质量 TQ  (20)  —— 20日回归斜率 + R²（趋势是否稳健）
  3. 量能配合 VOL (20)  —— 量比、成交额、放量方向
  4. 形态健康 FORM(15)  —— 位置、乖离、K线形态、是否过热
  5. 风险控制 RISK(20)  —— 波动率/回撤/ATR 反向打分（稳健优先）

设计取向：**在系统性下跌日，优先"抗跌 + 有承接 + 不过热"的品种**，
而非单纯追逐涨幅榜（涨幅榜里往往已透支、次日回撤风险大）。
"""
import json
from pathlib import Path

import numpy as np

TODAY = "2026-10-14"
OUT = Path(__file__).resolve().parent.parent / "outputs"
MKT = -1.77          # 今日全市场平均涨跌
MKT_MED = -2.00      # 中位数

feats = json.loads((OUT / f"tech_features_{TODAY}.json").read_text(encoding="utf-8"))


def clip01(x, lo, hi):
    return max(0.0, min(1.0, (x - lo) / (hi - lo)))


def score_one(r):
    # ---- 1) 相对强度 (25) ----
    # 相对今日市场的超额（抗跌/逆势）
    excess_today = (r["pct"] or 0) - MKT
    s_excess = clip01(excess_today, 0, 10)          # 跑赢 0%~10%
    # 中期动量（20日），过高视为透支 -> 用钟形更合适，这里用分段
    r20 = r["r20"] or 0
    if r20 <= 40:
        s_r20 = clip01(r20, -5, 40)
    else:
        s_r20 = max(0.2, 1 - (r20 - 40) / 160)      # >40% 逐级降权
    # 短期动量（5日）
    s_r5 = clip01(r["r5"] or 0, -10, 15)
    RS = 25 * (0.5 * s_excess + 0.3 * s_r20 + 0.2 * s_r5)

    # ---- 2) 趋势质量 (20) ----
    # 斜率（%/日）与 R² 组合：趋势既要有方向，也要稳健
    s_slope = clip01(r["slope20"], 0, 1.5)
    s_r2 = clip01(r["r2_20"], 0.3, 0.85)
    # 多头排列持续时间：越久越可靠（但过久也可能见顶，这里主要奖励确认）
    s_days = clip01(r["bull_days"], 0, 30)
    TQ = 20 * (0.45 * s_slope + 0.40 * s_r2 + 0.15 * s_days)

    # ---- 3) 量能配合 (20) ----
    # 温和放量最佳：1.0~2.0 最优，过度放量（>3）警惕出货
    vr = r["vol_ratio5_20"] or 1
    if vr < 1.0:
        s_vr = clip01(vr, 0.5, 1.0) * 0.7
    elif vr <= 2.0:
        s_vr = clip01(vr, 1.0, 2.0) * 0.5 + 0.5
    else:
        s_vr = max(0.25, 1 - (vr - 2.0) / 3.0)
    # 成交额（流动性）：太小难进难出
    s_amt = clip01(r["amt20_yi"] or 0, 0.3, 3.0)
    # 今日是否放量（承接力度）
    lvr = r["last_vol_ratio"] or 1
    s_lvr = clip01(lvr, 0.6, 2.5)
    VOL = 20 * (0.40 * s_vr + 0.30 * s_amt + 0.30 * s_lvr)

    # ---- 4) 形态健康 (15) ----
    # 位置：60日区间 60~95% 较理想（趋势中段），>95 追高风险
    p60 = r["pos60"] or 50
    if p60 <= 95:
        s_pos = clip01(p60, 50, 95)
    else:
        s_pos = max(0.35, 1 - (p60 - 95) / 10)
    # 乖离率：20日乖离 <15% 健康，>30% 严重超买
    bias = r["ma_bias20"] or 0
    s_bias = clip01(15 - bias, -15, 12) if bias > 0 else 0.5
    if bias <= 15:
        s_bias = 1.0
    elif bias <= 30:
        s_bias = 1 - (bias - 15) / 15 * 0.6
    else:
        s_bias = max(0.1, 0.4 - (bias - 30) / 50 * 0.4)
    # 今日收盘位于振幅中的位置（越高说明尾盘有承接）
    s_cp = clip01(r["close_pos"], 30, 95)
    FORM = 15 * (0.40 * s_pos + 0.40 * s_bias + 0.20 * s_cp)

    # ---- 5) 风险控制 (20) ----
    # 波动率低优先（稳健）
    s_vol = clip01(90 - (r["vol20"] or 60), 0, 70)
    # 60日最大回撤小优先
    s_mdd = clip01(-(r["mdd60"] or -15), 0, 25)
    # ATR 适中（太低无弹性，太高难控风险）
    ap = r["atr_pct"] or 3
    if ap <= 8:
        s_atr = clip01(ap, 0.5, 8) * 0.6 + 0.4
    else:
        s_atr = max(0.2, 1 - (ap - 8) / 8)
    # 过热惩罚：RSI6 > 90 或 J > 100
    overheat = 0
    if (r["rsi6"] or 0) > 90:
        overheat += 1
    if (r["kdj_j"] or 0) > 100:
        overheat += 1
    s_heat = 1.0 - 0.35 * overheat
    RISK = 20 * (0.35 * s_vol + 0.30 * s_mdd + 0.20 * s_atr + 0.15 * s_heat)

    total = RS + TQ + VOL + FORM + RISK
    return {
        "RS": round(RS, 2), "TQ": round(TQ, 2), "VOL": round(VOL, 2),
        "FORM": round(FORM, 2), "RISK": round(RISK, 2), "total": round(total, 2),
        "overheat": overheat,
    }


# 行业/名称
import sqlite3
con = sqlite3.connect(str(Path(__file__).resolve().parent.parent / "data" / "quant.db"), timeout=120)
meta = {r[0]: {"name": r[1], "industry": r[2]} for r in
        con.execute("SELECT symbol,name,industry FROM stock_info")}
con.close()

rows = []
for r in feats:
    sc = score_one(r)
    m = meta.get(r["symbol"], {})
    rows.append({**r, **sc, "name": m.get("name"), "industry": m.get("industry")})

rows.sort(key=lambda x: -x["total"])
(OUT / f"ranked_scores_{TODAY}.json").write_text(
    json.dumps(rows, ensure_ascii=False, indent=1), encoding="utf-8")

print(f"{'排名':>3} {'代码':<11}{'名称':<10}{'行业':<10}{'收盘':>8}{'今涨%':>7}{'总分':>7}"
      f"{'RS':>6}{'TQ':>6}{'VOL':>6}{'FORM':>6}{'RISK':>6}  {'过热':>3}")
for i, r in enumerate(rows[:20], 1):
    print(f"{i:>3} {r['symbol']:<11}{(r['name'] or ''):<10}{(r['industry'] or ''):<10}"
          f"{r['close']:>8.2f}{(r['pct'] or 0):>7.2f}{r['total']:>7.1f}"
          f"{r['RS']:>6.1f}{r['TQ']:>6.1f}{r['VOL']:>6.1f}{r['FORM']:>6.1f}{r['RISK']:>6.1f}  {r['overheat']:>3}")
print(f"\n共 {len(rows)} 只已保存 -> outputs/ranked_scores_{TODAY}.json")