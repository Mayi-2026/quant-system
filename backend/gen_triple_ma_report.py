"""生成 2026-10-14 三周期多头排列 HTML 报告"""
import json
from collections import Counter
from pathlib import Path

TODAY = "2026-10-14"
OUT = Path(__file__).resolve().parent.parent / "outputs"
res = json.loads((OUT / f"triple_ma_bullish_{TODAY}.json").read_text(encoding="utf-8"))
fun = json.loads((OUT / f"funnel_{TODAY}.json").read_text(encoding="utf-8"))

# 按行业分组排序
res = sorted(res, key=lambda r: (r["industry"] or "zzz", r["symbol"]))
ind = Counter(r["industry"] or "未分类" for r in res)
up = sum(1 for r in res if (r["pct"] or 0) > 0)
down = sum(1 for r in res if (r["pct"] or 0) < 0)
flat = len(res) - up - down


def pctcls(v):
    if v is None:
        return "flat"
    return "up" if v > 0 else ("down" if v < 0 else "flat")


def pcttxt(v):
    return "-" if v is None else f"{v:+.2f}%"


rows = []
for i, r in enumerate(res, 1):
    d, w, m = r["d_ma"], r["w_ma"], r["m_ma"]
    rows.append(f"""<tr>
<td class="idx">{i}</td>
<td class="sym">{r['symbol']}</td>
<td class="nm">{r['name']}</td>
<td class="ind">{r['industry'] or '—'}</td>
<td class="num">{r['close']:.2f}</td>
<td class="num {pctcls(r['pct'])}">{pcttxt(r['pct'])}</td>
<td class="ma">MA5 {d['5']:.2f} &gt; MA10 {d['10']:.2f} &gt; MA20 {d['20']:.2f} &gt; MA60 {d['60']:.2f}</td>
<td class="ma">MA5 {w['5']:.2f} &gt; MA10 {w['10']:.2f} &gt; MA20 {w['20']:.2f}</td>
<td class="ma">MA5 {m['5']:.2f} &gt; MA10 {m['10']:.2f} &gt; MA20 {m['20']:.2f}</td>
</tr>""")

ind_rows = "\n".join(
    f'<span class="chip">{k}<b>{v}</b></span>' for k, v in ind.most_common()
)

html = f"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>A股日/周/月线三周期多头排列 · {TODAY}</title>
<style>
  :root {{
    --bg:#f5f6f8; --card:#fff; --ink:#1a1d23; --ink2:#5b6470; --ink3:#8b95a1;
    --line:#e6e9ee; --up:#d92b2b; --down:#0a8f43; --brand:#c8102e; --accent:#1f5fd6;
  }}
  * {{ box-sizing:border-box; }}
  body {{ margin:0; background:var(--bg); color:var(--ink);
    font:14px/1.6 -apple-system,"PingFang SC","Microsoft YaHei",sans-serif; }}
  .wrap {{ max-width:1280px; margin:0 auto; padding:28px 20px 60px; }}
  header {{ margin-bottom:22px; }}
  h1 {{ font-size:24px; margin:0 0 6px; letter-spacing:.5px; }}
  .sub {{ color:var(--ink3); font-size:13px; }}
  .badge {{ display:inline-block; background:var(--brand); color:#fff; font-size:12px;
    padding:2px 9px; border-radius:10px; margin-left:8px; vertical-align:middle; }}
  .cards {{ display:grid; grid-template-columns:repeat(auto-fit,minmax(150px,1fr));
    gap:12px; margin:20px 0 8px; }}
  .card {{ background:var(--card); border:1px solid var(--line); border-radius:12px;
    padding:14px 16px; }}
  .card .k {{ color:var(--ink3); font-size:12px; }}
  .card .v {{ font-size:23px; font-weight:700; margin-top:3px; }}
  .card .v.sm {{ font-size:15px; font-weight:600; color:var(--ink2); }}
  .card.hl {{ border-color:#f3c8cf; background:#fff7f8; }}
  .card.hl .v {{ color:var(--brand); }}
  .note {{ background:#fffdf3; border:1px solid #f0e3b8; border-radius:10px;
    padding:12px 15px; font-size:12.5px; color:#6b5a1e; margin:16px 0; }}
  .note b {{ color:#8a6d00; }}
  h2 {{ font-size:15px; margin:24px 0 10px; color:var(--ink2); }}
  .chips {{ display:flex; flex-wrap:wrap; gap:7px; }}
  .chip {{ background:var(--card); border:1px solid var(--line); border-radius:16px;
    padding:3px 11px; font-size:12.5px; color:var(--ink2); }}
  .chip b {{ color:var(--brand); margin-left:5px; }}
  table {{ width:100%; border-collapse:collapse; background:var(--card);
    border:1px solid var(--line); border-radius:12px; overflow:hidden;
    font-size:12.5px; margin-top:10px; }}
  th {{ background:#fafbfc; color:var(--ink2); font-weight:600; text-align:left;
    padding:10px 11px; border-bottom:1px solid var(--line); white-space:nowrap;
    position:sticky; top:0; }}
  td {{ padding:9px 11px; border-bottom:1px solid #f2f4f7; vertical-align:top; }}
  tr:last-child td {{ border-bottom:none; }}
  tr:hover td {{ background:#fafcff; }}
  .idx {{ color:var(--ink3); width:38px; }}
  .sym {{ font-family:ui-monospace,Menlo,monospace; color:var(--accent); white-space:nowrap; }}
  .nm {{ font-weight:600; white-space:nowrap; }}
  .ind {{ color:var(--ink2); white-space:nowrap; }}
  .num {{ text-align:right; font-family:ui-monospace,Menlo,monospace; white-space:nowrap; }}
  .up {{ color:var(--up); font-weight:600; }}
  .down {{ color:var(--down); font-weight:600; }}
  .flat {{ color:var(--ink3); }}
  .ma {{ font-family:ui-monospace,Menlo,monospace; color:var(--ink2); font-size:11.5px; }}
  footer {{ margin-top:26px; color:var(--ink3); font-size:11.5px; line-height:1.8; }}
</style>
</head>
<body>
<div class="wrap">
<header>
  <h1>A股日 / 周 / 月线 三周期多头排列<span class="badge">{TODAY} 收盘</span></h1>
  <div class="sub">多头排列定义：日线 MA5&gt;MA10&gt;MA20&gt;MA60，周线 MA5&gt;MA10&gt;MA20，月线 MA5&gt;MA10&gt;MA20（均线自上而下依次排列）</div>
</header>

<div class="cards">
  <div class="card hl"><div class="k">三周期同时多头排列</div><div class="v">{len(res)}</div><div class="k">只</div></div>
  <div class="card"><div class="k">全市场样本</div><div class="v">{fun['total']}</div><div class="k">只 A 股</div></div>
  <div class="card"><div class="k">占比</div><div class="v">{len(res)/fun['total']*100:.1f}<span style="font-size:14px">%</span></div></div>
  <div class="card"><div class="k">今日表现</div><div class="v sm"><span class="up">涨 {up}</span> / <span class="down">跌 {down}</span> / <span class="flat">平 {flat}</span></div></div>
</div>

<div class="note">
<b>筛选漏斗：</b>全市场 {fun['total']} 只 → 日线多头 <b>{fun['d']}</b> 只 → 日+周 <b>{fun['dw']}</b> 只 →
日+月 <b>{fun['dm']}</b> 只 → 周+月 <b>{fun['wm']}</b> 只 → <b>三周期同时多头 {fun['dwm']} 只</b>。
（另有 {fun['short']} 只因历史不足 20 个月线被排除）
</div>

<h2>行业分布</h2>
<div class="chips">
{ind_rows}
</div>

<h2>入选个股（{len(res)} 只）</h2>
<table>
<thead><tr>
<th>#</th><th>代码</th><th>名称</th><th>行业</th><th>收盘</th><th>涨跌</th>
<th>日线均线排列</th><th>周线均线排列</th><th>月线均线排列</th>
</tr></thead>
<tbody>
{''.join(rows)}
</tbody>
</table>

<footer>
数据来源：本地 quant.db 日线历史 + 腾讯行情今日（{TODAY}）收盘补齐。<br>
口径提示：今日为周三，本周/本月 K 线尚未走完，当前周线、月线按最新收盘实时计算（与通达信等行情软件一致）。<br>
前复权口径：本地历史为长周期序列，个别个股近期除权会使趋势略有偏差，交易前请以行情软件复核。<br>
本报告仅为技术面形态统计，不构成任何投资建议。
</footer>
</div>
</body>
</html>"""

(OUT / f"三周期多头排列_{TODAY}.html").write_text(html, encoding="utf-8")
print("已生成:", OUT / f"三周期多头排列_{TODAY}.html")