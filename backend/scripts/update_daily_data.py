#!/usr/bin/env python3
"""将指定交易日全市场收盘数据入库（标准口径 upsert）。

用法:
    python update_daily_data.py 2026-10-14

数据来源优先级:
    1) outputs/full_quotes_<date>.json（当日已抓取的收盘快照，含完整 OHLCV）
    2) 腾讯行情 qt.gtimg.cn 实时补抓（补齐换手率与 JSON 缺失标的）

口径换算（腾讯源 -> 库内标准）:
    volume: 手 -> 股  (×100)
    amount: 万元 -> 元 (×1e4)
    turnover: %（直接用）
    pct_change: %（直接用）

写入: quant.db / market_data，按 (symbol, trade_date) upsert，
      覆盖当日盘中快照，缺成交额/换手率的历史记录一并补全。
"""
import json
import sqlite3
import sys
import time
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent.parent
DB = ROOT / "data" / "quant.db"
OUT_DIR = ROOT / "outputs"

UA = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
      "Referer": "https://qt.gtimg.cn/"}


def to_tencent_code(symbol: str) -> str:
    code, _, suf = symbol.partition(".")
    suf = suf.upper()
    if suf == "SH":
        return f"sh{code}"
    if suf == "SZ":
        return f"sz{code}"
    return f"sh{code}" if code[:1] in ("6", "5") else f"sz{code}"


def fetch_tencent(symbols, batch=300, sleep=0.15):
    """批量抓取腾讯收盘行情，返回 {symbol: {...标准口径...}}"""
    out = {}
    for i in range(0, len(symbols), batch):
        chunk = symbols[i:i + batch]
        codes = ",".join(to_tencent_code(s) for s in chunk)
        url = f"https://qt.gtimg.cn/q={codes}"
        try:
            req = urllib.request.Request(url, headers=UA)
            raw = urllib.request.urlopen(req, timeout=20).read().decode("gbk", "ignore")
        except Exception as e:
            print(f"  [warn] batch {i} 失败: {e}")
            continue
        lines = [l for l in raw.strip().split("\n") if "=" in l]
        for j, line in enumerate(lines):
            if j >= len(chunk):
                break
            sym = chunk[j]
            f = line.split("=", 1)[1].strip('";').split("~")
            if len(f) < 45:
                continue
            def num(idx):
                try:
                    return float(f[idx]) if f[idx] not in ("", "-") else None
                except (ValueError, IndexError):
                    return None
            close = num(3)
            if not close:
                continue
            out[sym] = {
                "name": f[1],
                "close": close,
                "prev_close": num(4),
                "open": num(5),
                "high": num(33),
                "low": num(34),
                "volume": (num(6) or 0) * 100.0,        # 手 -> 股
                "amount": (num(37) or 0) * 1e4,         # 万元 -> 元
                "turnover": num(38),                    # %
                "pct_change": num(32),                  # %
            }
        if (i // batch) % 4 == 0:
            print(f"  ...已抓取 {min(i+batch, len(symbols))}/{len(symbols)}")
        time.sleep(sleep)
    return out


def main():
    date = sys.argv[1] if len(sys.argv) > 1 else "2026-10-14"
    src = OUT_DIR / f"full_quotes_{date}.json"
    base = json.loads(src.read_text(encoding="utf-8")) if src.exists() else {}
    print(f"基准 JSON: {src.name} -> {len(base)} 只")

    con = sqlite3.connect(str(DB), timeout=180)
    symbols = [r[0] for r in con.execute(
        "SELECT symbol FROM stock_info WHERE market IN ('SSE','SZSE') ORDER BY symbol"
    ).fetchall()]
    print(f"目标样本(SSE/SZSE): {len(symbols)} 只")

    print("重新抓取腾讯收盘（补齐换手率）...")
    t0 = time.time()
    live = fetch_tencent(symbols)
    print(f"腾讯抓取完成: {len(live)} 只, 耗时 {time.time()-t0:.1f}s")

    # 合并：live 优先（含 turnover），JSON 回退
    merged = {}
    for sym in symbols:
        rec = live.get(sym)
        j = base.get(sym)
        if rec and rec.get("close"):
            if j and j.get("name"):
                rec["name"] = j["name"]
            merged[sym] = rec
        elif j and j.get("close"):
            merged[sym] = {
                "name": j.get("name", ""),
                "close": j["close"], "prev_close": j.get("prev_close"),
                "open": j.get("open"), "high": j.get("high"), "low": j.get("low"),
                "volume": (j.get("volume") or 0) * 100.0,
                "amount": (j.get("amount") or 0) * 1e4,
                "turnover": None, "pct_change": j.get("pct"),
            }
    print(f"合并后可得当日数据: {len(merged)} / {len(symbols)} 只")

    # close 一致性校验（live vs JSON）
    diff = []
    for sym in merged:
        if sym in live and sym in base and base[sym].get("close"):
            if live[sym]["close"] and abs(live[sym]["close"] - base[sym]["close"]) > 1e-6:
                diff.append((sym, base[sym]["close"], live[sym]["close"]))
    print(f"close 口径差异(live vs JSON): {len(diff)} 只")
    for d in diff[:10]:
        print("   ", d)

    # upsert
    cur = con.cursor()
    ins = upd = 0
    for sym, v in merged.items():
        row = cur.execute(
            "SELECT id FROM market_data WHERE symbol=? AND trade_date=?", (sym, date)
        ).fetchone()
        if row:
            cur.execute(
                """UPDATE market_data SET open=?,high=?,low=?,close=?,volume=?,amount=?,
                   turnover=COALESCE(?,turnover), pct_change=? WHERE id=?""",
                (v["open"], v["high"], v["low"], v["close"], v["volume"],
                 v["amount"], v["turnover"], v["pct_change"], row[0]),
            )
            upd += 1
        else:
            cur.execute(
                """INSERT INTO market_data
                   (symbol,trade_date,open,high,low,close,volume,amount,turnover,pct_change,created_at)
                   VALUES (?,?,?,?,?,?,?,?,?,?,datetime('now'))""",
                (sym, date, v["open"], v["high"], v["low"], v["close"],
                 v["volume"], v["amount"], v["turnover"], v["pct_change"]),
            )
            ins += 1
    con.commit()

    print(f"入库完成: 新增 {ins} 条, 更新 {upd} 条")
    cnt = cur.execute("SELECT COUNT(*) FROM market_data WHERE trade_date=?", (date,)).fetchone()[0]
    amt = cur.execute("SELECT SUM(amount) FROM market_data WHERE trade_date=?", (date,)).fetchone()[0]
    turn_cov = cur.execute(
        "SELECT SUM(CASE WHEN turnover IS NOT NULL THEN 1 ELSE 0 END) FROM market_data WHERE trade_date=?",
        (date,)).fetchone()[0]
    print(f"校验: {date} 共 {cnt} 条, amount合计={amt/1e12:.4f} 万亿元, 换手率覆盖={turn_cov}/{cnt}")
    con.close()

    # 同步一份到 Sequoia-X（供其选股策略消费）
    try:
        import sys as _sys
        _backend = ROOT / "backend"
        if str(_backend) not in _sys.path:
            _sys.path.insert(0, str(_backend))
        from services import sequoia_sync
        res = sequoia_sync.sync_recent(days=30)
        print(f"[Sequoia-X] 已同步 {res.get('synced', 0)} 条")
    except Exception as e:
        print(f"[Sequoia-X] 同步失败: {e}")


if __name__ == "__main__":
    main()