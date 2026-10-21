"""多头股票池筛选 + 回测报告生成

供回测执行器在每次回测完成后调用：
- screen_daily_bullish()：筛选日线多头排列（MA5>MA10>MA20>MA60）的股票池，
  并按技术面综合评分排序，返回前 top_n 只。
- generate_report_html()：将回测指标 + 股票池渲染为独立 HTML 报告文件。

复用 metrics：日线 K 线取 MarketData，股票基础信息取 StockInfo。
"""
import json
import logging
from datetime import datetime
from pathlib import Path

import pandas as pd

from config import BASE_DIR
from models.market_data import MarketData, StockInfo

logger = logging.getLogger(__name__)

REPORTS_DIR = BASE_DIR / "data" / "reports"


def calculate_ma(df, periods=(5, 10, 20, 60)):
    for p in periods:
        df[f"ma{p}"] = df["close"].rolling(window=p).mean()
    return df


def calculate_macd(df, fast=12, slow=26, signal=9):
    ema_fast = df["close"].ewm(span=fast, adjust=False).mean()
    ema_slow = df["close"].ewm(span=slow, adjust=False).mean()
    df["dif"] = ema_fast - ema_slow
    df["dea"] = df["dif"].ewm(span=signal, adjust=False).mean()
    df["macd"] = 2 * (df["dif"] - df["dea"])
    return df


def calculate_rsi(df, period=14):
    delta = df["close"].diff()
    gain = (delta.where(delta > 0, 0)).rolling(window=period).mean()
    loss = (-delta.where(delta < 0, 0)).rolling(window=period).mean()
    rs = gain / loss
    df["rsi"] = 100 - (100 / (1 + rs))
    return df


def calculate_volume_ratio(df, period=5):
    df["vol_ma"] = df["volume"].rolling(window=period).mean()
    df["volume_ratio"] = df["volume"] / df["vol_ma"]
    return df


def is_bullish_alignment(row):
    try:
        if row["ma5"] != row["ma5"] or row["ma10"] != row["ma10"] \
                or row["ma20"] != row["ma20"] or row["ma60"] != row["ma60"]:
            return False
        return row["ma5"] > row["ma10"] > row["ma20"] > row["ma60"]
    except Exception:
        return False


def _technical_score(df):
    """技术面综合评分（0~100）"""
    score = 0
    latest = df.iloc[-1]
    prev = df.iloc[-2] if len(df) > 1 else latest

    if latest["dif"] > latest["dea"] and prev["dif"] <= prev["dea"]:
        score += 20
    elif latest["dif"] > latest["dea"]:
        score += 10

    if latest["macd"] > 0 and latest["macd"] > prev["macd"]:
        score += 10

    rsi = latest["rsi"]
    if 40 <= rsi <= 70:
        score += 15
    elif 30 <= rsi < 40 or 70 < rsi <= 80:
        score += 5

    if latest["volume_ratio"] > 1.5:
        score += 15
    elif latest["volume_ratio"] > 1.0:
        score += 5

    if latest["close"] > latest["ma5"] > latest["ma10"] > latest["ma20"]:
        score += 10

    pct = (latest["close"] - prev["close"]) / prev["close"] * 100 if prev["close"] > 0 else 0
    if 0 < pct <= 5:
        score += 10
    elif -2 <= pct <= 0:
        score += 5

    return score, latest


def _get_daily_df(db, symbol, days=120):
    rows = (
        db.query(MarketData)
        .filter(MarketData.symbol == symbol)
        .order_by(MarketData.trade_date.desc())
        .limit(days)
        .all()
    )
    if not rows:
        return None

    df = pd.DataFrame([{
        "date": r.trade_date,
        "close": float(r.close or 0),
        "volume": float(r.volume or 0),
    } for r in rows])
    df["date"] = pd.to_datetime(df["date"])
    df = df.sort_values("date").reset_index(drop=True)
    return df


def screen_daily_bullish(db, top_n=10):
    """筛选日线多头排列股票池，按技术评分降序返回前 top_n 只。

    返回 (stock_pool: list[dict], total_count: int)。
    说明：只分析沪深两市，排除北交所标的。
    """
    from datasources import standards as std

    stocks = [s for s in db.query(StockInfo).all() if std.is_tradable_a_stock(s.symbol)]
    candidates = []

    for stock in stocks:
        df = _get_daily_df(db, stock.symbol)
        if df is None or len(df) < 60:
            continue
        df = calculate_ma(df)
        df = calculate_macd(df)
        df = calculate_rsi(df)
        df = calculate_volume_ratio(df)
        if not is_bullish_alignment(df.iloc[-1]):
            continue

        score, latest = _technical_score(df)
        prev_close = df.iloc[-2]["close"] if len(df) > 1 else latest["close"]
        pct_change = (latest["close"] - prev_close) / prev_close * 100 if prev_close > 0 else 0.0

        candidates.append({
            "symbol": stock.symbol,
            "name": stock.name,
            "industry": stock.industry or "未知",
            "close": round(float(latest["close"]), 2),
            "pct_change": round(float(pct_change), 2),
            "tech_score": int(score),
            "rsi": round(float(latest["rsi"]), 1),
            "macd": round(float(latest["macd"]), 3),
            "volume_ratio": round(float(latest["volume_ratio"]), 2),
            "ma5": round(float(latest["ma5"]), 2),
            "ma10": round(float(latest["ma10"]), 2),
            "ma20": round(float(latest["ma20"]), 2),
            "ma60": round(float(latest["ma60"]), 2),
        })

    candidates.sort(key=lambda x: x["tech_score"], reverse=True)
    total = len(candidates)
    return candidates[:top_n], total


def generate_report_html(pool, meta):
    """生成独立 HTML 回测报告，返回绝对路径。

    meta 需包含：strategy_name, symbol, start_date, end_date, metrics(dict)。
    """
    REPORTS_DIR.mkdir(parents=True, exist_ok=True)
    filename = f"backtest_{meta.get('backtest_id') or datetime.now().strftime('%Y%m%d%H%M%S')}.html"
    path = REPORTS_DIR / filename

    metrics = meta.get("metrics") or {}

    def pct(v):
        return f"{v * 100:+.2f}%" if isinstance(v, (int, float)) else "-"

    pool_rows = ""
    if pool:
        for i, s in enumerate(pool, 1):
            pool_rows += (
                f"<tr><td>{i}</td><td>{s['symbol']}</td><td>{s['name']}</td>"
                f"<td>{s.get('industry', '')}</td>"
                f"<td class='num'>{s['close']:.2f}</td>"
                f"<td class='num'>{s['pct_change']:+.2f}%</td>"
                f"<td class='num'>{s['tech_score']}</td>"
                f"<td class='num mono'>{s['ma5']:.2f}</td>"
                f"<td class='num mono'>{s['ma10']:.2f}</td>"
                f"<td class='num mono'>{s['ma20']:.2f}</td>"
                f"<td class='num mono'>{s['ma60']:.2f}</td></tr>"
            )
    else:
        pool_rows = '<tr><td colspan="11" class="empty">当前市场无日线多头排列股票</td></tr>'

    html = f"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>回测报告 · {meta.get('strategy_name') or '策略'}</title>
<style>
  body{{font-family:-apple-system,BlinkMacSystemFont,"PingFang SC","Microsoft YaHei",sans-serif;color:#1A1D24;background:#F4F5F7;margin:0;padding:32px;line-height:1.6}}
  .wrap{{max-width:980px;margin:0 auto}}
  h1{{font-size:22px;margin:0 0 4px}}
  .sub{{color:#8A8F99;font-size:13px;margin-bottom:24px}}
  .card{{background:#fff;border:1px solid #E7E9EE;border-radius:14px;padding:20px;margin-bottom:16px;box-shadow:0 1px 2px rgba(16,24,40,.04)}}
  .grid{{display:grid;grid-template-columns:repeat(4,1fr);gap:12px}}
  .m{{background:#F7F9FC;border-radius:10px;padding:12px;text-align:center}}
  .m .label{{font-size:12px;color:#8A8F99}}
  .m .val{{font-size:18px;font-weight:700;margin-top:4px;font-variant-numeric:tabular-nums}}
  table{{width:100%;border-collapse:collapse;font-size:13px}}
  th{{text-align:left;font-size:12px;color:#8A8F99;padding:8px;border-bottom:1px solid #E7E9EE}}
  td{{padding:8px;border-bottom:1px solid #F0F2F5}}
  .num{{text-align:right;font-variant-numeric:tabular-nums}}
  .mono{{font-family:"SF Mono",ui-monospace,Menlo,monospace}}
  .empty{{text-align:center;color:#8A8F99;padding:24px}}
  .h{{font-size:15px;font-weight:600;margin-bottom:12px}}
</style>
</head>
<body>
<div class="wrap">
  <h1>回测报告 · {meta.get('strategy_name') or '-'}</h1>
  <div class="sub">标的 {meta.get('symbol') or '-'} · {meta.get('start_date') or '-'} ~ {meta.get('end_date') or '-'}</div>

  <div class="card">
    <div class="grid">
      <div class="m"><div class="label">累计收益</div><div class="val">{pct(metrics.get('total_return'))}</div></div>
      <div class="m"><div class="label">年化收益</div><div class="val">{pct(metrics.get('annual_return'))}</div></div>
      <div class="m"><div class="label">最大回撤</div><div class="val">{pct(metrics.get('max_drawdown'))}</div></div>
      <div class="m"><div class="label">夏普比率</div><div class="val">{metrics.get('sharpe_ratio') if metrics.get('sharpe_ratio') is not None else '-'}</div></div>
      <div class="m"><div class="label">胜率</div><div class="val">{pct(metrics.get('win_rate'))}</div></div>
      <div class="m"><div class="label">交易次数</div><div class="val">{metrics.get('trade_count') or 0}</div></div>
    </div>
  </div>

  <div class="card">
    <div class="h">日线多头股票池（MA5&gt;MA10&gt;MA20&gt;MA60）</div>
    <table>
      <thead><tr>
        <th>#</th><th>代码</th><th>名称</th><th>行业</th>
        <th class="num">现价</th><th class="num">涨跌幅</th><th class="num">技术评分</th>
        <th class="num">MA5</th><th class="num">MA10</th><th class="num">MA20</th><th class="num">MA60</th>
      </tr></thead>
      <tbody>{pool_rows}</tbody>
    </table>
  </div>
</div>
</body>
</html>"""

    path.write_text(html, encoding="utf-8")
    return str(path)