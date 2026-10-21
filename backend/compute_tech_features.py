"""对 73 只三周期多头排列个股做技术面 + 资金面特征计算（2026-10-14）

输出 outputs/tech_features_2026-10-14.json
每一只计算：多周期动量、趋势强度/位置、量能、超买超卖、波动、形态等。
今日收盘用 outputs/today_quotes_2026-10-14.json（腾讯）补齐。
"""
import json
import sqlite3
from pathlib import Path

import numpy as np
import pandas as pd

TODAY = "2026-10-14"
OUT = Path(__file__).resolve().parent.parent / "outputs"
DB = Path(__file__).resolve().parent.parent / "data" / "quant.db"


def ema(s, n):
    return s.ewm(span=n, adjust=False).mean()


def rsi(s, n=14):
    """RSI：无下跌时按 100 处理（否则除零会得到 NaN）。"""
    d = s.diff()
    up = d.clip(lower=0).rolling(n).mean()
    dn = (-d.clip(upper=0)).rolling(n).mean()
    out = 100 - 100 / (1 + up / dn.replace(0, np.nan))
    out = out.where(dn != 0, 100.0)   # 无下跌 -> 100
    out = out.where(up != 0, 0.0)     # 无上涨 -> 0
    return out


def macd(s, f=12, sl=26, sig=9):
    dif = ema(s, f) - ema(s, sl)
    dea = ema(dif, sig)
    return dif, dea, (dif - dea) * 2


def atr(high, low, close, n=14):
    pc = close.shift(1)
    tr = pd.concat([(high - low), (high - pc).abs(), (low - pc).abs()], axis=1).max(axis=1)
    return tr.rolling(n).mean()


def kdj(high, low, close, n=9):
    ln = low.rolling(n).min()
    hn = high.rolling(n).max()
    rsv = (close - ln) / (hn - ln).replace(0, np.nan) * 100
    k = rsv.ewm(com=2, adjust=False).mean()
    d = k.ewm(com=2, adjust=False).mean()
    return k, d, 3 * k - 2 * d


def boll(close, n=20, k=2):
    m = close.rolling(n).mean()
    sd = close.rolling(n).std()
    return m + k * sd, m, m - k * sd


def main():
    res = json.loads((OUT / f"triple_ma_bullish_{TODAY}.json").read_text(encoding="utf-8"))
    syms = [r["symbol"] for r in res]
    con = sqlite3.connect(str(DB), timeout=180)
    ph = ",".join("?" * len(syms))
    df = pd.read_sql(
        f"SELECT symbol,trade_date,open,high,low,close,volume,amount,turnover,pct_change "
        f"FROM market_data WHERE symbol IN ({ph}) AND close IS NOT NULL ORDER BY symbol,trade_date",
        con, params=syms,
    )
    con.close()
    df["trade_date"] = pd.to_datetime(df["trade_date"])

    # 用今日完整行情（含 OHLCV）补齐/覆盖最后一行
    # 口径换算：腾讯 volume=手(x100=股)，amount=万元(x1e4=元)，与本地库一致
    quotes = json.loads((OUT / f"full_quotes_{TODAY}.json").read_text(encoding="utf-8"))
    aug = []
    for s in syms:
        v = quotes.get(s)
        if not v:
            continue
        prev = df[(df.symbol == s) & (df.trade_date < pd.Timestamp(TODAY))]
        if prev.empty:
            continue
        p = prev.iloc[-1]
        aug.append({
            "symbol": s, "trade_date": pd.Timestamp(TODAY),
            "open": v.get("open") or p["close"], "high": v.get("high") or p["close"],
            "low": v.get("low") or p["close"], "close": v["close"],
            "volume": (v.get("volume") or 0) * 100,        # 手 -> 股
            "amount": (v.get("amount") or 0) * 1e4,         # 万元 -> 元
            "turnover": p.get("turnover"),
            "pct_change": v.get("pct"),
        })
    df = pd.concat([df[df.trade_date < pd.Timestamp(TODAY)], pd.DataFrame(aug)], ignore_index=True)

    feats = []
    for sym, g in df.groupby("symbol", sort=False):
        g = g.sort_values("trade_date").reset_index(drop=True)
        c, h, l, v = g["close"], g["high"], g["low"], g["volume"]
        amt = g["amount"]
        n = len(c)
        if n < 60:
            continue
        last = float(c.iloc[-1])

        # --- 动量 ---
        def ret(k):
            return float(c.iloc[-1] / c.iloc[-1 - k] - 1) * 100 if n > k else None

        r1, r3, r5, r10, r20, r60 = (ret(x) for x in (1, 3, 5, 10, 20, 60))

        # --- 均线系统 ---
        ma5, ma10, ma20, ma60 = (float(c.rolling(x).mean().iloc[-1]) for x in (5, 10, 20, 60))
        ma_bias20 = (last / ma20 - 1) * 100          # 20日乖离率
        ma_bias60 = (last / ma60 - 1) * 100
        # 多头排列持续天数（MA5>MA10>MA20>MA60）
        bull = (c.rolling(5).mean() > c.rolling(10).mean()) & \
               (c.rolling(10).mean() > c.rolling(20).mean()) & \
               (c.rolling(20).mean() > c.rolling(60).mean())
        bull_days = int(bull.iloc[::-1].cumprod().sum()) if bull.notna().any() else 0

        # --- 趋势强度：线性回归斜率 R² ---
        y = c.iloc[-20:].values
        x = np.arange(len(y))
        slope, inter = np.polyfit(x, y, 1)
        yhat = slope * x + inter
        ss_res = ((y - yhat) ** 2).sum()
        ss_tot = ((y - y.mean()) ** 2).sum()
        r2_20 = float(1 - ss_res / ss_tot) if ss_tot > 0 else 0
        slope_pct = float(slope / y.mean() * 100)  # 日均斜率%

        # --- 位置 ---
        hi60, lo60 = float(c.iloc[-60:].max()), float(c.iloc[-60:].min())
        pos60 = (last - lo60) / (hi60 - lo60) * 100 if hi60 > lo60 else 50
        hi250 = float(c.max())
        dd_from_high = (last / hi250 - 1) * 100

        # --- 量能 ---
        v5, v20 = float(v.iloc[-5:].mean()), float(v.iloc[-20:].mean())
        vol_ratio = v5 / v20 if v20 else 1
        last_vol_ratio = float(v.iloc[-1] / v20) if v20 else 1
        # 今日缩量/放量 vs 20日均
        amt5 = float(amt.iloc[-5:].mean()) if amt.notna().any() else None
        amt20 = float(amt.iloc[-20:].mean()) if amt.notna().any() else None

        # --- 超买超卖 ---
        rsi6 = float(rsi(c, 6).iloc[-1])
        rsi14 = float(rsi(c, 14).iloc[-1])
        k, d, j = kdj(h, l, c)
        kdj_k, kdj_d, kdj_j = float(k.iloc[-1]), float(d.iloc[-1]), float(j.iloc[-1])
        dif, dea, hist = macd(c)
        macd_dif, macd_dea, macd_hist = float(dif.iloc[-1]), float(dea.iloc[-1]), float(hist.iloc[-1])
        macd_hist_prev = float(hist.iloc[-2])
        macd_golden = macd_dif > macd_dea and macd_hist > macd_hist_prev  # 金叉且红柱放大
        above_zero = macd_dif > 0

        # --- 波动率 ---
        atr14 = float(atr(h, l, c, 14).iloc[-1])
        atr_pct = atr14 / last * 100
        ret20 = c.pct_change().iloc[-20:]
        vol20 = float(ret20.std() * np.sqrt(252) * 100)  # 年化波动%
        # 最大回撤（近60日）
        cm = c.iloc[-60:].cummax()
        mdd60 = float(((c.iloc[-60:] / cm - 1).min()) * 100)

        # --- 形态 ---
        up, mid, low_band = boll(c)
        boll_pos = (last - float(low_band.iloc[-1])) / (float(up.iloc[-1]) - float(low_band.iloc[-1])) * 100
        # 是否创60日新高
        new_high60 = last >= hi60 - 1e-9
        # 今日 K 线实体与影线（相对当日最高/最低价）
        o = float(g["open"].iloc[-1])
        hi_t, lo_t = float(h.iloc[-1]), float(l.iloc[-1])
        rng = hi_t - lo_t
        body = (last - o) / o * 100
        if rng > 0:
            upper_shadow = (hi_t - max(o, last)) / rng * 100   # 上影占振幅比
            lower_shadow = (min(o, last) - lo_t) / rng * 100   # 下影占振幅比
        else:
            upper_shadow = lower_shadow = 0.0
        # 收盘位于当日振幅中的位置（0=最低 100=最高）
        close_pos = (last - lo_t) / rng * 100 if rng > 0 else 50.0
        # 今日振幅
        amp = (float(h.iloc[-1]) - float(l.iloc[-1])) / float(c.iloc[-2]) * 100 if n >= 2 else 0
        # 连涨/连跌天数
        pc = g["pct_change"].iloc[-10:].values
        streak = 0
        for x in pc[::-1]:
            if x is None or np.isnan(x):
                break
            if (x > 0 and (streak >= 0)) or (x < 0 and (streak <= 0)):
                streak += 1 if x > 0 else -1
            else:
                break

        # --- 相对强度 vs 大盘（用中证1000近似，这里用全市场平均） ---
        feats.append({
            "symbol": sym,
            "close": round(last, 3),
            "pct": round(r1, 2) if r1 is not None else None,
            "r3": round(r3, 2) if r3 is not None else None,
            "r5": round(r5, 2) if r5 is not None else None,
            "r10": round(r10, 2) if r10 is not None else None,
            "r20": round(r20, 2) if r20 is not None else None,
            "r60": round(r60, 2) if r60 is not None else None,
            "ma5": round(ma5, 3), "ma10": round(ma10, 3),
            "ma20": round(ma20, 3), "ma60": round(ma60, 3),
            "ma_bias20": round(ma_bias20, 2), "ma_bias60": round(ma_bias60, 2),
            "bull_days": bull_days,
            "slope20": round(slope_pct, 3), "r2_20": round(r2_20, 3),
            "pos60": round(pos60, 1), "dd_from_high": round(dd_from_high, 1),
            "vol_ratio5_20": round(vol_ratio, 2),
            "last_vol_ratio": round(last_vol_ratio, 2),
            "amt20_yi": round(amt20 / 1e8, 3) if amt20 else None,
            "rsi6": round(rsi6, 1), "rsi14": round(rsi14, 1),
            "kdj_k": round(kdj_k, 1), "kdj_d": round(kdj_d, 1), "kdj_j": round(kdj_j, 1),
            "macd_dif": round(macd_dif, 3), "macd_dea": round(macd_dea, 3),
            "macd_hist": round(macd_hist, 3), "macd_golden": bool(macd_golden),
            "macd_above0": bool(above_zero),
            "atr_pct": round(atr_pct, 2), "vol20": round(vol20, 1), "mdd60": round(mdd60, 1),
            "boll_pos": round(boll_pos, 1),
            "new_high60": bool(new_high60),
            "body": round(body, 2), "upper_shadow": round(upper_shadow, 2),
            "lower_shadow": round(lower_shadow, 2), "close_pos": round(close_pos, 1),
            "amp": round(amp, 2),
            "streak": int(streak),
        })

    (OUT / f"tech_features_{TODAY}.json").write_text(
        json.dumps(feats, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"已计算 {len(feats)} 只技术特征 -> outputs/tech_features_{TODAY}.json")
    return feats


if __name__ == "__main__":
    main()