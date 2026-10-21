"""全市场「日线 + 周线 + 月线」三周期均线多头排列扫描（SQL 窗口函数版）

口径（与通达信 / 同花顺一致）：
- 日线多头排列：MA5 > MA10 > MA20 > MA60
- 周线多头排列：MA5 > MA10 > MA20
- 月线多头排列：MA5 > MA10 > MA20
- 当周 / 当月 K 线未走完，取最新交易日收盘作为该周期当前值
- 月线不足 20 根（上市不足约 2 年）剔除；当日无成交（停牌）剔除；ST 单独标记

实现要点：均线全部在 SQLite 内用窗口函数算，Python 侧只接收每只股票的 1 行结果，
避免把 600 万行行情整表读进内存（pandas pivot 会吃 1.8GB 被系统 OOM kill）。

用法：
    python triple_ma_scan.py
    python triple_ma_scan.py --date 2026-10-19
    python triple_ma_scan.py --no-st-filter
"""
from __future__ import annotations

import argparse
import html
import json
import sqlite3
from pathlib import Path

import pandas as pd

BASE = Path(__file__).resolve().parent.parent
DB = BASE / "data" / "quant.db"
OUT_DIR = BASE / "outputs"

MIN_MONTH_BARS = 20


def board_of(code: str) -> str:
    if code.startswith(("688", "689")):
        return "科创板"
    if code.startswith(("300", "301")):
        return "创业板"
    if code.startswith("60"):
        return "沪主板"
    if code.startswith("00"):
        return "深主板"
    return "其他"


WEEK_KEY = "date(trade_date, 'weekday 4')"   # 该周周五（周六日不交易）
MONTH_KEY = "strftime('%Y-%m', trade_date)"  # 自然月


def _last_ma_sql(key: str | None) -> str:
    """生成某周期「最新一根 bar 的 MA5/10/20(+/60)」查询。key=None 表示日线。"""
    part = f"symbol, {key}" if key else "symbol"
    order = f"{key}, trade_date" if key else "trade_date"
    if key:
        return f"""
        WITH b AS (
          SELECT symbol, k, close,
                 ROW_NUMBER() OVER (PARTITION BY symbol, k ORDER BY trade_date DESC) AS rk
          FROM (SELECT symbol, {key} AS k, trade_date, close FROM market_data WHERE close > 0)
        ),
        bars AS (SELECT symbol, k, close FROM b WHERE rk = 1),
        aggs AS (
          SELECT symbol, k, close,
                 AVG(close) OVER w20 AS ma20,
                 AVG(close) OVER w10 AS ma10,
                 AVG(close) OVER w5  AS ma5,
                 COUNT(*)   OVER w20 AS c20,
                 COUNT(*)   OVER w10 AS c10,
                 COUNT(*)   OVER w5  AS c5,
                 COUNT(*)   OVER (PARTITION BY symbol) AS bars,
                 ROW_NUMBER() OVER (PARTITION BY symbol ORDER BY k DESC) AS rn
          FROM bars
          WINDOW w20 AS (PARTITION BY symbol ORDER BY k ROWS BETWEEN 19 PRECEDING AND CURRENT ROW),
                 w10 AS (PARTITION BY symbol ORDER BY k ROWS BETWEEN 9 PRECEDING AND CURRENT ROW),
                 w5  AS (PARTITION BY symbol ORDER BY k ROWS BETWEEN 4 PRECEDING AND CURRENT ROW)
        )
        SELECT symbol, ma20, ma10, ma5, c20, c10, c5, bars FROM aggs WHERE rn = 1
        """

    return f"""
    WITH d AS (
      SELECT symbol, trade_date, close,
             AVG(close) OVER (PARTITION BY symbol ORDER BY trade_date ROWS BETWEEN 59 PRECEDING AND CURRENT ROW) AS ma60,
             AVG(close) OVER (PARTITION BY symbol ORDER BY trade_date ROWS BETWEEN 19 PRECEDING AND CURRENT ROW) AS ma20,
             AVG(close) OVER (PARTITION BY symbol ORDER BY trade_date ROWS BETWEEN 9  PRECEDING AND CURRENT ROW) AS ma10,
             AVG(close) OVER (PARTITION BY symbol ORDER BY trade_date ROWS BETWEEN 4  PRECEDING AND CURRENT ROW) AS ma5,
             COUNT(*)    OVER (PARTITION BY symbol ORDER BY trade_date ROWS BETWEEN 59 PRECEDING AND CURRENT ROW) AS c60,
             COUNT(*)    OVER (PARTITION BY symbol ORDER BY trade_date ROWS BETWEEN 19 PRECEDING AND CURRENT ROW) AS c20,
             COUNT(*)    OVER (PARTITION BY symbol ORDER BY trade_date ROWS BETWEEN 9  PRECEDING AND CURRENT ROW) AS c10,
             COUNT(*)    OVER (PARTITION BY symbol ORDER BY trade_date ROWS BETWEEN 4  PRECEDING AND CURRENT ROW) AS c5,
             ROW_NUMBER() OVER (PARTITION BY symbol ORDER BY trade_date DESC) AS rk
      FROM market_data WHERE close > 0
    )
    SELECT symbol, ma60, ma20, ma10, ma5, c60, c20, c10, c5 FROM d WHERE rk = 1
    """


def scan(as_of: str | None = None, st_filter: bool = True):
    con = sqlite3.connect(str(DB), timeout=300)
    con.execute("PRAGMA temp_store = FILE")   # 注意：MEMORY 会让 600 万行的窗口排序 OOM
    con.execute("PRAGMA cache_size = -64000")

    where = ""
    params: tuple = ()
    if as_of:
        where = "WHERE trade_date <= ?"
        params = (as_of,)

    # 最新交易日
    last_date = con.execute(f"SELECT MAX(trade_date) FROM market_data {'WHERE trade_date <= ?' if as_of else ''}",
                            params).fetchone()[0]
    if not last_date:
        raise SystemExit("market_data 为空")

    # 市场快照（当日）
    snap = pd.read_sql(
        f"""SELECT m.symbol, m.close, m.pct_change, m.volume, i.name, i.industry, i.is_st
            FROM market_data m LEFT JOIN stock_info i ON i.symbol = m.symbol
            WHERE m.trade_date = ? AND i.market IN ('SSE','SZSE')""",
        con, params=(last_date,))

    daily = pd.read_sql(_last_ma_sql(None).replace("WHERE close > 0",
                        "WHERE close > 0" + (f" AND trade_date <= '{as_of}'" if as_of else "")), con)
    weekly = pd.read_sql(_last_ma_sql(WEEK_KEY).replace("WHERE close > 0",
                         "WHERE close > 0" + (f" AND trade_date <= '{as_of}'" if as_of else "")), con)
    monthly = pd.read_sql(_last_ma_sql(MONTH_KEY).replace("WHERE close > 0",
                          "WHERE close > 0" + (f" AND trade_date <= '{as_of}'" if as_of else "")), con)
    con.close()

    df = snap.merge(daily, on="symbol", how="inner") \
             .merge(weekly.rename(columns={c: f"w_{c}" for c in weekly.columns if c != "symbol"}), on="symbol",
                    how="inner") \
             .merge(monthly.rename(columns={c: f"m_{c}" for c in monthly.columns if c != "symbol"}), on="symbol",
                    how="inner")

    n_universe = len(df)
    traded = (df["volume"].fillna(0) > 0).sum()

    daily_ok = (df.c60 >= 60) & (df.c20 >= 20) & (df.c10 >= 10) & (df.c5 >= 5) & \
               (df.ma5 > df.ma10) & (df.ma10 > df.ma20) & (df.ma20 > df.ma60)
    weekly_ok = (df.w_c20 >= 20) & (df.w_c10 >= 10) & (df.w_c5 >= 5) & \
                (df.w_ma5 > df.w_ma10) & (df.w_ma10 > df.w_ma20)
    monthly_ok = (df.m_c20 >= 20) & (df.m_c10 >= 10) & (df.m_c5 >= 5) & \
                 (df.m_ma5 > df.m_ma10) & (df.m_ma10 > df.m_ma20)
    liquid = df["volume"].fillna(0) > 0

    hit = df[daily_ok & weekly_ok & monthly_ok & liquid].copy()
    hit["code"] = hit["symbol"].str.split(".").str[0]
    hit["board"] = hit["code"].map(board_of)
    hit["name"] = hit["name"].fillna("").str.replace(" ", "", regex=False)
    hit["is_st"] = ((hit["is_st"].fillna(0).astype(int) == 1) | hit["name"].str.upper().str.contains("ST")).astype(int)

    stats = {
        "as_of": last_date,
        "universe": int(n_universe),
        "traded": int(traded),
        "daily_only": int(daily_ok.sum()),
        "weekly_and_daily": int((daily_ok & weekly_ok).sum()),
        "hit_before_st": int(len(hit)),
    }

    # 当日是否为「盘中快照」：若是，均线用的是未收盘价，结论只能当盘中参考
    try:
        con2 = sqlite3.connect(str(DB), timeout=60)
        prov = con2.execute(
            "SELECT COUNT(*) FROM market_data WHERE trade_date = ? AND COALESCE(is_final, 1) = 0",
            (last_date,)).fetchone()[0]
        con2.close()
    except Exception:
        prov = 0
    stats["provisional_rows"] = int(prov)

    if st_filter:
        hit = hit[hit["is_st"] == 0]
    stats["hit_after_st"] = int(len(hit))

    hit["dev20"] = (hit["close"] / hit["ma20"] - 1) * 100
    hit = hit.sort_values(["ma5", "close"], ascending=False) if False else hit
    hit = hit.sort_values("dev20", ascending=False)
    return hit, stats


def render_html(df: pd.DataFrame, stats: dict) -> str:
    tag = stats["as_of"]
    rows = []
    for _, r in df.iterrows():
        st = '<span class="tag st">ST</span>' if r["is_st"] else ""
        pct = r["pct_change"]
        cls = "up" if (pct is not None and pct == pct and pct > 0) else ("down" if (pct is not None and pct == pct and pct < 0) else "")
        pct_txt = f"{pct:+.2f}%" if pct is not None and pct == pct else "—"
        rows.append(f"""<tr>
<td class="code">{r['code']}</td><td class="nm">{html.escape(r['name'])} {st}</td>
<td><span class="chip">{html.escape(str(r['board']))}</span></td>
<td class="ind">{html.escape(str(r['industry'] or '—'))}</td>
<td class="num">{r['close']:.2f}</td><td class="num {cls}">{pct_txt}</td>
<td class="num">{r['ma5']:.2f}</td><td class="num">{r['ma10']:.2f}</td>
<td class="num">{r['ma20']:.2f}</td><td class="num">{r['ma60']:.2f}</td>
<td class="num">{r['dev20']:+.1f}%</td>
<td class="num">{r['w_ma5']:.2f} / {r['w_ma20']:.2f}</td>
<td class="num">{r['m_ma5']:.2f} / {r['m_ma20']:.2f}</td>
</tr>""")

    ind = df["industry"].fillna("未分类").replace("", "未分类").value_counts().head(10)
    maxc = int(ind.max()) if len(ind) else 1
    bars = "".join(
        f'<div class="bar-row"><span class="bar-label">{html.escape(str(k))}</span>'
        f'<span class="bar" style="width:{max(4, v / maxc * 100):.0f}%"></span><span class="bar-val">{v}</span></div>'
        for k, v in ind.items())

    boards = df["board"].value_counts()
    board_txt = " · ".join(f"{k} {v}" for k, v in boards.items())

    return f"""<!DOCTYPE html>
<html lang="zh-CN"><head><meta charset="utf-8">
<title>三周期均线多头排列 · {tag}</title>
<style>
*{{box-sizing:border-box}}
body{{margin:0;padding:32px 28px 60px;background:#f6f7f9;color:#1c2024;
font:14px/1.6 -apple-system,BlinkMacSystemFont,"PingFang SC","Microsoft YaHei",sans-serif}}
.wrap{{max-width:1280px;margin:0 auto}}
h1{{font-size:24px;margin:0 0 6px;letter-spacing:-.3px}}
.sub{{color:#6b7480;font-size:13px;margin-bottom:22px}}
.cards{{display:grid;grid-template-columns:repeat(5,1fr);gap:12px;margin-bottom:22px}}
.card{{background:#fff;border:1px solid #e6e9ee;border-radius:10px;padding:14px 16px}}
.card .k{{font-size:12px;color:#7a828d;margin-bottom:6px}}
.card .v{{font-size:22px;font-weight:650;letter-spacing:-.5px}}
.card .v small{{font-size:12px;font-weight:400;color:#8b939e;margin-left:4px}}
.panel{{background:#fff;border:1px solid #e6e9ee;border-radius:10px;padding:18px 18px 6px;margin-bottom:18px}}
.panel h2{{font-size:14px;margin:0 0 12px;color:#4a525c;font-weight:600}}
.bar-row{{display:flex;align-items:center;gap:10px;margin-bottom:7px;font-size:12.5px}}
.bar-label{{width:96px;color:#5b636d;flex:none}}
.bar{{height:9px;background:linear-gradient(90deg,#c8402f,#e8776a);border-radius:5px;min-width:4px}}
.bar-val{{color:#8b939e;font-size:12px}}
table{{width:100%;border-collapse:collapse;background:#fff;font-size:13px;
border:1px solid #e6e9ee;border-radius:10px;overflow:hidden}}
thead th{{background:#fafbfc;color:#7a828d;font-weight:600;font-size:12px;
text-align:left;padding:10px 10px;border-bottom:1px solid #eceff3;white-space:nowrap}}
td{{padding:9px 10px;border-bottom:1px solid #f2f4f7}}
tbody tr:hover{{background:#fcfbfa}}
td.num{{text-align:right;font-variant-numeric:tabular-nums;font-feature-settings:"tnum"}}
td.code{{font-family:ui-monospace,Menlo,monospace;color:#4a525c}}
td.nm{{font-weight:600;white-space:nowrap}}
td.ind{{color:#6b7480;font-size:12.5px}}
.up{{color:#c8402f}} .down{{color:#1a8f5a}}
.chip{{display:inline-block;padding:1px 7px;border-radius:5px;background:#f1f3f6;color:#6b7480;font-size:11.5px}}
.tag.st{{background:#fdecea;color:#c8402f;font-size:11px;padding:0 5px;border-radius:4px;font-weight:600}}
.foot{{margin-top:16px;color:#9aa1ab;font-size:12px;line-height:1.7}}
</style></head><body><div class="wrap">
<h1>日线 · 周线 · 月线 三周期均线多头排列</h1>
<div class="sub">数据截至 {tag} 收盘 · 全市场 A 股（沪深）· 口径：日 MA5&gt;MA10&gt;MA20&gt;MA60，周/月 MA5&gt;MA10&gt;MA20</div>
<div class="cards">
<div class="card"><div class="k">三周期同时满足</div><div class="v">{stats['hit_after_st']}<small>只</small></div></div>
<div class="card"><div class="k">仅日线多头</div><div class="v">{stats['daily_only']}<small>只</small></div></div>
<div class="card"><div class="k">日线+周线共振</div><div class="v">{stats['weekly_and_daily']}<small>只</small></div></div>
<div class="card"><div class="k">样本 A 股</div><div class="v">{stats['universe']}<small>只</small></div></div>
<div class="card"><div class="k">当日有成交</div><div class="v">{stats['traded']}<small>只</small></div></div>
</div>
<div class="panel"><h2>行业分布（前 10）</h2>{bars or '<div class="sub">—</div>'}</div>
<table><thead><tr>
<th>代码</th><th>名称</th><th>板块</th><th>行业</th><th style="text-align:right">收盘</th>
<th style="text-align:right">涨跌</th><th style="text-align:right">MA5</th><th style="text-align:right">MA10</th>
<th style="text-align:right">MA20</th><th style="text-align:right">MA60</th>
<th style="text-align:right">偏离MA20</th><th style="text-align:right">周线 MA5/20</th><th style="text-align:right">月线 MA5/20</th>
</tr></thead><tbody>{''.join(rows)}</tbody></table>
<div class="foot">板块构成：{html.escape(board_txt)}<br>
已剔除 ST 与当日停牌；上市不足约 2 年（月线 &lt; 20 根）不参与。按「偏离 MA20」从高到低排序，偏离越大越偏向短期强势。<br>
均线采用不复权收盘价（库内原始口径），除权日附近均线可能失真，请结合复权行情复核。</div>
</div></body></html>"""


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--date", default=None)
    ap.add_argument("--no-st-filter", action="store_true")
    args = ap.parse_args()

    df, stats = scan(args.date, st_filter=not args.no_st_filter)
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    tag = stats["as_of"]

    if df.empty:
        print(f"[{tag}] 三周期同时多头排列：0 只")
        return

    cols = ["symbol", "code", "name", "board", "industry", "is_st", "close", "pct_change",
            "ma5", "ma10", "ma20", "ma60", "dev20", "w_ma5", "w_ma10", "w_ma20",
            "m_ma5", "m_ma10", "m_ma20", "m_bars"]
    out = df[[c for c in cols if c in df.columns]].copy()
    out.columns = [{"pct_change": "pct", "m_bars": "month_bars"}.get(c, c) for c in out.columns]
    for c in ("close", "ma5", "ma10", "ma20", "ma60", "dev20", "w_ma5", "w_ma10", "w_ma20",
              "m_ma5", "m_ma10", "m_ma20", "pct"):
        if c in out:
            out[c] = out[c].astype(float).round(2)

    out.to_csv(OUT_DIR / f"三周期多头排列_{tag}.csv", index=False, encoding="utf-8-sig")
    (OUT_DIR / f"triple_ma_scan_{tag}.json").write_text(
        json.dumps({"stats": stats, "rows": out.to_dict("records")}, ensure_ascii=False, indent=1),
        encoding="utf-8")
    html_path = OUT_DIR / f"三周期多头排列_{tag}.html"
    html_path.write_text(render_html(df, stats), encoding="utf-8")

    print(f"=== {tag} 日/周/月三周期均线多头排列 ===")
    if stats.get("provisional_rows"):
        print(f"⚠️  当日有 {stats['provisional_rows']} 只仍是盘中快照（is_final=0），"
              f"收盘后请重新跑一次「更新数据」再扫描")
    print(f"样本={stats['universe']}  当日有成交={stats['traded']}  仅日线={stats['daily_only']}  "
          f"日+周={stats['weekly_and_daily']}  三周期={stats['hit_before_st']} 剔除ST后={stats['hit_after_st']}")
    print(out[["code", "name", "board", "industry", "close", "pct", "dev20"]].to_string(index=False))
    print(f"\n已生成：{html_path}")


if __name__ == "__main__":
    main()
