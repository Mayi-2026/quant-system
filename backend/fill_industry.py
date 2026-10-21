"""批量获取并填充股票行业分类信息"""
import sqlite3
import requests
import time
import sys
import os
from concurrent.futures import ThreadPoolExecutor, as_completed

DB_PATH = os.path.join(os.path.dirname(__file__), "..", "data", "quant.db")
API_URL = "https://emweb.securities.eastmoney.com/PC_HSF10/CompanySurvey/CompanySurveyAjax"
HEADERS = {"User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7)"}
MAX_WORKERS = 5
BATCH_SIZE = 50


def symbol_to_emcode(symbol: str) -> str:
    """将 600519.SH 转为 SH600519"""
    parts = symbol.split(".")
    code = parts[0]
    suffix = parts[1].upper() if len(parts) > 1 else ""
    return f"{suffix}{code}"


def fetch_industry(symbol: str) -> tuple:
    """获取单只股票的行业分类"""
    em_code = symbol_to_emcode(symbol)
    try:
        r = requests.get(API_URL, params={"code": em_code}, headers=HEADERS, timeout=10)
        data = r.json()
        industry = data.get("jbzl", {}).get("sshy", "")
        return (symbol, industry or "")
    except Exception as e:
        return (symbol, "")


def main():
    db_path = os.path.abspath(DB_PATH)
    print(f"数据库路径: {db_path}")
    conn = sqlite3.connect(db_path)
    c = conn.cursor()

    c.execute("SELECT COUNT(*) FROM stock_info WHERE industry IS NOT NULL AND industry != ''")
    already = c.fetchone()[0]
    c.execute("SELECT COUNT(*) FROM stock_info WHERE industry IS NULL OR industry = ''")
    todo = c.fetchone()[0]
    print(f"已有行业: {already}, 待填充: {todo}")

    if todo == 0:
        print("所有股票已有行业信息")
        conn.close()
        return

    c.execute("SELECT symbol FROM stock_info WHERE industry IS NULL OR industry = ''")
    symbols = [row[0] for row in c.fetchall()]
    total = len(symbols)

    updated = 0
    errors = 0
    start_time = time.time()

    for batch_start in range(0, total, BATCH_SIZE):
        batch = symbols[batch_start:batch_start + BATCH_SIZE]

        with ThreadPoolExecutor(max_workers=MAX_WORKERS) as executor:
            futures = {executor.submit(fetch_industry, sym): sym for sym in batch}
            update_list = []
            for future in as_completed(futures):
                sym, industry = future.result()
                if industry:
                    update_list.append((industry, sym))
                    updated += 1
                else:
                    errors += 1

            if update_list:
                c.executemany("UPDATE stock_info SET industry = ? WHERE symbol = ?", update_list)
                conn.commit()

        done = batch_start + len(batch)
        elapsed = time.time() - start_time
        speed = done / elapsed if elapsed > 0 else 0
        eta = (total - done) / speed if speed > 0 else 0
        print(f"[{done}/{total}] 成功:{updated} 失败:{errors} 速度:{speed:.1f}/s 剩余:{eta:.0f}s", flush=True)

    conn.close()
    print(f"\n完成! 更新 {updated}, 失败 {errors}, 耗时 {time.time()-start_time:.1f}s")


if __name__ == "__main__":
    main()
