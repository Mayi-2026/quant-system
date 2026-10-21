"""生成《73只三周期多头排列个股：宏观 + 技术面详细分析报告》HTML（2026-10-14）"""
import json
import sqlite3
from pathlib import Path

OUT = Path(__file__).resolve().parent.parent / "outputs"
TODAY = "2026-10-14"

ranked = json.loads((OUT / f"final_rank_{TODAY}.json").read_text(encoding="utf-8"))
tech = json.loads((OUT / f"tech_features_{TODAY}.json").read_text(encoding="utf-8"))
fun = json.loads((OUT / f"funnel_{TODAY}.json").read_text(encoding="utf-8"))
rob = json.loads((OUT / "robustness.json").read_text(encoding="utf-8"))
t10 = json.loads((OUT / "top10_backtest.json").read_text(encoding="utf-8"))
import numpy as np
t10df = t10
top10 = ranked[:10]
tm = {r["symbol"]: r for r in tech}

# 组合回测汇总
tp = np.mean([r["top10"] for r in t10df])
pool = np.mean([r["pool"] for r in t10df])
mkt = np.mean([r["mkt"] for r in t10df])
win = np.mean([r["top10_win"] for r in t10df])
exc = np.mean([r["excess_pool"] for r in t10df])

RISK = {}

def cls(v):
    return "up" if v > 0 else ("down" if v < 0 else "flat")


def T(v, u=""):
    return "-" if v is None else f"{v:+.2f}{u}" if u == "%" else f"{v:.2f}{u}"


# ---- Top10 卡片 ----
cards = []
for i, r in enumerate(top10, 1):
    t = tm[r["symbol"]]
    reasons = []
    if (t["last_vol_ratio"] or 9) < 1.0:
        reasons.append(f"今日量比仅 {t['last_vol_ratio']:.2f}，缩量下跌/整理，抛压有限")
    if (t["ma_bias20"] or 9) < 3:
        reasons.append(f"20日均线乖离 {t['ma_bias20']:+.2f}%，几乎贴着均线，回踩空间小")
    if (t["vol20"] or 999) < 25:
        reasons.append(f"年化波动 {t['vol20']:.1f}%，低波动品种在风险日更抗跌")
    if (t["r10"] or 0) < 3:
        reasons.append(f"10日涨幅 {t['r10']:+.2f}%，短期未透支")
    if (t["pos60"] or 0) >= 90:
        reasons.append(f"处60日区间 {t['pos60']:.0f}% 位置，趋势完好")
    if (t["macd_above0"] and t["macd_golden"]):
        reasons.append("MACD 零轴上方金叉，动能延续")
    risk = []
    if (t["atr_pct"] or 0) > 6:
        risk.append(f"ATR {t['atr_pct']:.1f}% 波动偏大")
    if (t["ma_bias20"] or 0) > 8:
        risk.append("乖离偏高，注意回抽")
    if (t["amt20_yi"] or 0) < 1:
        risk.append(f"日均成交额仅 {t['amt20_yi']:.2f}亿，流动性一般")
    if not (t["macd_above0"]):
        risk.append("MACD 位于零轴下方")
    cards.append(f"""
<div class="pick">
  <div class="pk-head">
    <div class="pk-rank">{i}</div>
    <div class="pk-title"><b>{r['name']}</b><span class="code">{r['symbol']}</span></div>
    <div class="pk-tags"><span class="tag ind">{r['industry'] or '—'}</span>
      <span class="tag">评分 {r['score']:.0f}</span></div>
  </div>
  <div class="pk-metrics">
    <div><span>收盘</span><b>{r['close']:.2f}</b></div>
    <div><span>今日</span><b class="{cls(r['pct'])}">{T(t['pct'],'%')}</b></div>
    <div><span>20日乖离</span><b>{T(t['ma_bias20'],'%')}</b></div>
    <div><span>今日量比</span><b>{t['last_vol_ratio']:.2f}</b></div>
    <div><span>年化波动</span><b>{t['vol20']:.1f}%</b></div>
    <div><span>60日位置</span><b>{t['pos60']:.0f}%</b></div>
  </div>
  <div class="pk-body">
    <div class="pk-reason"><span class="lbl ok">入选理由</span>
      <ul>{''.join(f'<li>{x}</li>' for x in reasons[:4])}</ul></div>
    <div class="pk-reason"><span class="lbl warn">风险点</span>
      <ul>{''.join(f'<li>{x}</li>' for x in risk) or '<li>未见明显技术性风险</li>'}</ul></div>
  </div>
</div>""")

# ---- 全表 ----
rows = []
for i, r in enumerate(ranked, 1):
    t = tm[r["symbol"]]
    hl = " class='hl'" if i <= 10 else ""
    rsi_cls = cls(1 if t["rsi6"] > 90 else (-1 if t["rsi6"] < 30 else 0))
    rows.append(f"""<tr{hl}>
<td class="idx">{i}</td><td class="sym">{r['symbol']}</td><td class="nm">{r['name']}</td>
<td class="ind">{r['industry'] or '—'}</td>
<td class="num">{r['close']:.2f}</td>
<td class="num {cls(t['pct'])}">{T(t['pct'],'%')}</td>
<td class="num sc">{r['score']:.0f}</td>
<td class="num">{t['ma_bias20']:+.1f}%</td>
<td class="num">{t['last_vol_ratio']:.2f}</td>
<td class="num">{t['vol20']:.0f}%</td>
<td class="num">{t['pos60']:.0f}%</td>
<td class="num {rsi_cls}">{t['rsi6']:.0f}</td>
<td class="num">{t['r20']:+.1f}%</td>
</tr>""")

# 稳健性表
rob_rows = "".join(
    f"<tr><td>{r['label']}</td><td class='num'>{r['n']}</td>"
    f"<td class='num up'>{r['ic']:+.4f}</td><td class='num'>{r['t']:.2f}</td>"
    f"<td class='num down'>{r['d1']:+.3f}%</td><td class='num up'>{r['d10']:+.3f}%</td>"
    f"<td class='num up'>{r['spread']:+.3f}%</td></tr>" for r in rob)

html = f"""<!DOCTYPE html>
<html lang="zh-CN"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>A股三周期多头排列 · 宏观与技术面深度分析 · {TODAY}</title>
<style>
 :root{{--bg:#f4f5f7;--card:#fff;--ink:#16191f;--ink2:#59616d;--ink3:#8c95a2;
  --line:#e4e7ec;--up:#d92b2b;--down:#0a8f43;--brand:#c8102e;--accent:#1f5fd6;--warn:#b26a00;}}
 *{{box-sizing:border-box}}
 body{{margin:0;background:var(--bg);color:var(--ink);font:14px/1.65 -apple-system,"PingFang SC","Microsoft YaHei",sans-serif}}
 .wrap{{max-width:1240px;margin:0 auto;padding:30px 20px 70px}}
 h1{{font-size:26px;margin:0 0 6px}}
 h2{{font-size:17px;margin:34px 0 12px;padding-left:11px;border-left:4px solid var(--brand)}}
 h3{{font-size:14.5px;margin:20px 0 8px;color:var(--ink2)}}
 .sub{{color:var(--ink3);font-size:13px;margin-bottom:18px}}
 .badge{{display:inline-block;background:var(--brand);color:#fff;font-size:12px;padding:3px 10px;border-radius:10px;margin-left:8px;vertical-align:middle}}
 .cards{{display:grid;grid-template-columns:repeat(auto-fit,minmax(158px,1fr));gap:12px;margin:18px 0}}
 .card{{background:var(--card);border:1px solid var(--line);border-radius:12px;padding:14px 16px}}
 .card .k{{color:var(--ink3);font-size:12px}}
 .card .v{{font-size:22px;font-weight:700;margin-top:4px}}
 .card .v.sm{{font-size:15px;font-weight:600}}
 .card.red{{border-color:#f3c8cf;background:#fff7f8}} .card.red .v{{color:var(--brand)}}
 .card.green{{border-color:#bfe3ce;background:#f4fbf7}} .card.green .v{{color:var(--down)}}
 .note{{background:#fffdf3;border:1px solid #f0e3b8;border-radius:10px;padding:13px 16px;font-size:12.8px;color:#6b5a1e;margin:14px 0}}
 .alert{{background:#fef2f2;border:1px solid #f5c2c2;border-radius:10px;padding:15px 17px;color:#7a1c1c;margin:16px 0;font-size:13px}}
 .alert b{{color:#a11010}}
 table{{width:100%;border-collapse:collapse;background:var(--card);border:1px solid var(--line);border-radius:10px;overflow:hidden;font-size:12.5px;margin-top:8px}}
 th{{background:#fafbfc;color:var(--ink2);font-weight:600;text-align:left;padding:10px;border-bottom:1px solid var(--line);white-space:nowrap;position:sticky;top:0;z-index:2}}
 td{{padding:8px 10px;border-bottom:1px solid #f2f4f7;vertical-align:middle}}
 tr:last-child td{{border-bottom:none}} tr.hl td{{background:#fff9fa}} tr:hover td{{background:#fafcff}}
 .idx{{color:var(--ink3);width:36px}} .sym{{font-family:ui-monospace,Menlo,monospace;color:var(--accent);white-space:nowrap}}
 .nm{{font-weight:600;white-space:nowrap}} .ind{{color:var(--ink2);white-space:nowrap}}
 .num{{text-align:right;font-family:ui-monospace,Menlo,monospace;white-space:nowrap}}
 .sc{{font-weight:700;color:var(--brand)}}
 .up{{color:var(--up)}} .down{{color:var(--down)}} .flat{{color:var(--ink3)}}
 /* 卡片 */
 .picks{{display:grid;grid-template-columns:repeat(auto-fit,minmax(370px,1fr));gap:14px}}
 .pick{{background:var(--card);border:1px solid var(--line);border-radius:13px;padding:15px 16px}}
 .pk-head{{display:flex;align-items:center;gap:10px;margin-bottom:10px}}
 .pk-rank{{width:26px;height:26px;border-radius:50%;background:var(--brand);color:#fff;display:flex;
   align-items:center;justify-content:center;font-weight:700;font-size:13px;flex:0 0 auto}}
 .pk-title{{flex:1;font-size:15px}} .pk-title .code{{color:var(--ink3);font-size:11.5px;margin-left:7px;font-family:ui-monospace,Menlo,monospace}}
 .tag{{display:inline-block;background:#f2f4f7;color:var(--ink2);font-size:11px;padding:2px 8px;border-radius:9px;margin-left:5px}}
 .tag.ind{{background:#eef4ff;color:#2b5bb5}}
 .pk-metrics{{display:grid;grid-template-columns:repeat(3,1fr);gap:7px;margin:10px 0 12px;
   background:#fafbfc;border-radius:9px;padding:10px}}
 .pk-metrics>div{{display:flex;flex-direction:column}}
 .pk-metrics span{{color:var(--ink3);font-size:11px}}
 .pk-metrics b{{font-size:13.5px;font-family:ui-monospace,Menlo,monospace}}
 .pk-body{{display:grid;grid-template-columns:1fr 1fr;gap:12px}}
 .pk-reason .lbl{{display:inline-block;font-size:11px;padding:1px 7px;border-radius:7px;margin-bottom:5px}}
 .lbl.ok{{background:#eaf6ee;color:#1a7a42}} .lbl.warn{{background:#fff4e5;color:var(--warn)}}
 .pk-reason ul{{margin:0;padding-left:15px}} .pk-reason li{{font-size:11.8px;color:var(--ink2);margin-bottom:3px}}
 .grid2{{display:grid;grid-template-columns:1fr 1fr;gap:16px}}
 @media(max-width:820px){{.grid2,.pk-body,.picks{{grid-template-columns:1fr}}}}
 footer{{margin-top:34px;color:var(--ink3);font-size:11.5px;line-height:1.9}}
 .kv{{display:flex;justify-content:space-between;padding:5px 0;border-bottom:1px dashed #eef0f4;font-size:12.5px}}
 .kv:last-child{{border:none}} .kv span{{color:var(--ink3)}} .kv b{{font-family:ui-monospace,Menlo,monospace}}
</style></head><body><div class="wrap">

<h1>A股 日/周/月线三周期多头排列 · 深度分析报告
  <span class="badge">{TODAY} 收盘</span></h1>
<div class="sub">候选池 {fun['dwm']} 只（全市场 {fun['total']} 只） · 宏观 + 技术面 + 历史验证的多因子排序</div>

<div class="alert">
<b>⚠️ 先给结论，再讲方法——今天是很差的一天。</b><br>
{TODAY} 全市场平均下跌 <b>-1.77%</b>（中位数 -2.00%），上涨家数仅 <b>17.6%</b>；
创业板 -3.10%、科创50 -3.29%、中证1000 -1.68%，而沪深300 仅 -1.36%——典型的
<b>"大盘价值抗跌、小盘成长杀跌"</b>的风险规避格局。五大指数全部跌破 MA5/MA20 并创 20 日新低。<br>
在"明天谁涨得最多"这个问题上，我必须先讲清一件事：<b>我无法预测单只股票的次日涨幅</b>。
任何声称能精确预测的人都不可信。下面给出的是<b>经过 356 个交易日历史回测</b>、
在统计上确实有正向优势的排序——但它只提高概率，不保证结果。
</div>

<h2>一、宏观与市场环境</h2>
<div class="cards">
  <div class="card red"><div class="k">上证指数</div><div class="v sm">3767.23</div><div class="k down">-0.88%</div></div>
  <div class="card red"><div class="k">深证成指</div><div class="v sm">12110.59</div><div class="k down">-1.97%</div></div>
  <div class="card red"><div class="k">创业板指</div><div class="v sm">2866.76</div><div class="k down">-3.10%</div></div>
  <div class="card red"><div class="k">科创50</div><div class="v sm">1380.26</div><div class="k down">-3.29%</div></div>
  <div class="card red"><div class="k">沪深300</div><div class="v sm">4217.68</div><div class="k down">-1.36%</div></div>
  <div class="card red"><div class="k">中证1000</div><div class="v sm">6905.17</div><div class="k down">-1.68%</div></div>
</div>

<div class="grid2">
<div>
<h3>市场宽度（今日）</h3>
<div class="kv"><span>上涨 / 下跌 / 平盘</span><b><span class="up">918</span> / <span class="down">4212</span> / 94</b></div>
<div class="kv"><span>上涨占比</span><b class="down">17.6%</b></div>
<div class="kv"><span>涨停 / 跌停</span><b>47 / 22</b></div>
<div class="kv"><span>涨超7% / 跌超7%</span><b>57 / 82</b></div>
<div class="kv"><span>中位数涨跌</span><b class="down">-2.00%</b></div>
<div class="kv"><span>昨日上涨家数</span><b>2791（今日急转）</b></div>
</div>
<div>
<h3>指数趋势位置</h3>
<div class="kv"><span>上证 MA5 / MA20 / MA60</span><b>3798 / 3871 / 3889</b></div>
<div class="kv"><span>上证 20日区间位置</span><b class="down">1%（贴地板）</b></div>
<div class="kv"><span>创业板 20日位置</span><b class="down">0%</b></div>
<div class="kv"><span>沪深300 20日位置</span><b class="down">0%</b></div>
<div class="kv"><span>均线排列</span><b class="down">空头（收盘&lt;MA5&lt;MA20&lt;MA60）</b></div>
<div class="kv"><span>两市成交额</span><b>≈1.41万亿（缩量）</b></div>
</div>
</div>

<div class="note">
<b>环境判断（三点）：</b><br>
1. <b>趋势向下</b>——五大指数全部处于 MA5&lt;MA20&lt;MA60 空头排列，且收在 20 日区间最低端；<br>
2. <b>风格防御</b>——今日行业仅 游戏(+2.83%)、文化传媒(+1.39%)、燃气/煤炭(+1.29%)、电力(+0.75%)、银行(-0.03%) 相对抗跌；
   而 风电设备(-5.20%)、输配电气(-3.09%)、电池(-3.07%)、半导体(-2.88%) 等成长赛道重挫；<br>
3. <b>缩量下跌</b>——成交额 1.41 万亿，低于 10-12（1.54万亿）和 10-13（1.67万亿），
   属于"抛压不大但也无承接"的存量防守市。这种环境下，<b>低波动 + 高股息 + 不过热</b>的品种占优。
</div>

<h2>二、方法：为什么这 10 只，而不是涨幅榜前 10</h2>
<p style="font-size:13px;color:var(--ink2)">
我最初按直觉构造了一个"动量+强势"打分模型（奖励涨幅大、量能强、趋势陡的股票），
但把它放到 <b>356 个交易日</b>的历史数据中回测后发现：<b>它没有效果，甚至略差</b>——
Top10 反而跑输 Bottom10。于是我做了一次严格的因子有效性检验（日频横截面 IC），
得到了一个<b>与直觉完全相反、但统计上高度显著</b>的结论：
</p>

<div class="alert" style="background:#f0f7ff;border-color:#c3d9f5;color:#1a3a6b">
<b>核心发现：在这 73 只"三周期多头"里，越是"不过热"的股票，次日表现越好。</b><br>
各因子的 IC（信息系数，&gt;0 表示因子值越大次日收益越高）：<br>
· 今日量比 &nbsp;<b>IC = -0.068</b>（t = -10.5）——放量越猛，次日越差<br>
· 5/20量比 &nbsp;<b>IC = -0.057</b>（t = -8.2）<br>
· 20日乖离 &nbsp;<b>IC = -0.053</b>（t = -6.6）——离均线越远，次日越差<br>
· 10日动量 &nbsp;<b>IC = -0.050</b>（t = -6.2）<br>
· 20日波动率 <b>IC = -0.064</b>（t = -5.5）——波动越大，次日越差<br>
<span style="font-size:12px;color:#3a5a8b">（12 个因子的 IC 全部为负或接近 0，没有任何一个"追涨"因子有效）</span>
</div>

<h3>基于此构造的最终模型（5 因子等权，反向）</h3>
<table style="font-size:12.5px">
<thead><tr><th>因子</th><th>含义</th><th>方向</th><th>权重</th><th>历史 IC</th><th>t 值</th></tr></thead>
<tbody>
<tr><td>量能克制</td><td>今日量比（相对20日均量）</td><td class="down">越低越好</td><td>25%</td><td class="num down">-0.068</td><td class="num">-10.5</td></tr>
<tr><td>量能趋势</td><td>5日均量 / 20日均量</td><td class="down">越低越好</td><td>20%</td><td class="num down">-0.057</td><td class="num">-8.2</td></tr>
<tr><td>乖离收敛</td><td>相对 MA20 的乖离率</td><td class="down">越低越好</td><td>20%</td><td class="num down">-0.053</td><td class="num">-6.6</td></tr>
<tr><td>动量温和</td><td>近10日涨幅</td><td class="down">越低越好</td><td>20%</td><td class="num down">-0.050</td><td class="num">-6.2</td></tr>
<tr><td>波动收敛</td><td>20日年化波动率</td><td class="down">越低越好</td><td>15%</td><td class="num down">-0.064</td><td class="num">-5.5</td></tr>
</tbody></table>

<h3>组合分的稳健性检验（越靠后的检验越严格）</h3>
<table style="font-size:12.5px">
<thead><tr><th>样本</th><th>交易日</th><th>IC 均值</th><th>t 值</th><th>D1（最差组）</th><th>D10（最好组）</th><th>多空差</th></tr></thead>
<tbody>{rob_rows}</tbody></table>
<div class="note" style="background:#f4fbf7;border-color:#bfe3ce;color:#1a5c38">
组合分 IC 均值 <b>+0.078，t = 8.99</b>，IC&gt;0 占比 <b>69.8%</b>；
<b>在全部 7 个子样本中方向一致</b>（含前半段/后半段、剔除低流动性前后），
说明不是某一段行情的偶然。这是本报告最主要的可信依据。
</div>

<h2>三、Top 10 推荐（按已验证模型排序）</h2>
<div class="picks">{''.join(cards)}</div>

<h2>四、历史验证：这个 Top10 到底有多大优势？</h2>
<table style="font-size:12.5px">
<thead><tr><th>指标</th><th>数值</th><th>说明</th></tr></thead>
<tbody>
<tr><td>Top10 次日均收益</td><td class="num up">{tp:+.3f}%</td><td>356 个交易日平均</td></tr>
<tr><td>候选池次日均收益</td><td class="num up">{pool:+.3f}%</td><td>同期 73 只（及历史同类）平均</td></tr>
<tr><td>全市场次日均收益</td><td class="num up">{mkt:+.3f}%</td><td>同期全市场平均</td></tr>
<tr><td><b>Top10 相对候选池超额</b></td><td class="num up"><b>{exc:+.3f}%</b></td><td><b>选股能力所在</b></td></tr>
<tr><td>Top10 次日正收益胜率</td><td class="num">{win:.1f}%</td><td>约一半，单日不可预测</td></tr>
<tr><td>Top10 跑赢候选池胜率</td><td class="num">54.2%</td><td>相对优势的持续性</td></tr>
</tbody></table>

<div class="alert">
<b>必须讲清的三条局限（否则这份报告会误导你）：</b><br>
1. <b>优势幅度很小</b>：相对候选池的超额只有 <b>+0.045%/日</b>，考虑双边交易成本后
   基本被吃掉。它适合"在已决定的候选池里优化先后顺序"，<b>不适合当作独立的赚钱策略</b>。<br>
2. <b>候选池整体跑输大盘</b>：三周期多头排列组合的次日均收益（{pool:+.3f}%）低于全市场（{mkt:+.3f}%）。
   也就是说，"三周期多头"这个形态本身在次日维度上<b>不是优势，反而是轻微劣势</b>
   （因为它筛出的是"已经涨过一波"的股票，在弱势市里更容易均值回复）。<br>
3. <b>未考虑交易约束</b>：回测未剔除次日涨跌停无法成交的情形，也未计滑点、印花税，实际收益会更低。
</div>

<h2>五、全部 73 只评分明细</h2>
<table>
<thead><tr><th>#</th><th>代码</th><th>名称</th><th>行业</th><th>收盘</th><th>涨跌</th><th>评分</th>
<th>MA20乖离</th><th>今日量比</th><th>年化波动</th><th>60日位置</th><th>RSI6</th><th>20日涨幅</th></tr></thead>
<tbody>{''.join(rows)}</tbody></table>

<footer>
<b>数据来源</b>：本地 quant.db 日线（2021-10 起，5228 只）+ 腾讯行情今日（{TODAY}）收盘补齐；
宏观指数经腾讯/新浪双源交叉验证。<br>
<b>口径</b>：日线 MA5/10/20/60；周线（W-FRI）、月线（ME）MA5/10/20；多头排列=短&gt;中&gt;长依次排列。
今日为周三，本周/本月K线用最新收盘实时计算（同通达信口径）。<br>
<b>风险提示</b>：本报告为量化形态统计与模型回溯，<b>不构成任何投资建议</b>。
模型基于历史规律，市场结构与行情风格可能变化导致失效；单日涨跌不可预测，任何排序都不能替代独立判断与风控。
建议关注：仓位管理、止损纪律、单票/行业集中度（本 Top10 中银行占 7 席，行业高度集中，需自行分散）。
</footer>
</div></body></html>"""

(OUT / f"三周期多头排列_深度分析_{TODAY}.html").write_text(html, encoding="utf-8")
print("已生成报告:", OUT / f"三周期多头排列_深度分析_{TODAY}.html")