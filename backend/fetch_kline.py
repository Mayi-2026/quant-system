"""独立脚本：获取单只股票K线数据，输出 JSON 到 stdout。
优先使用新浪财经 API，失败时 fallback 到东方财富 API。
用法: python fetch_kline.py <code> <start_date> <end_date>
输出: JSON 数组 或 {"error": "..."}
"""
import sys
import json
import urllib.request
import ssl
import re


def fetch_with_sina(code: str, start_date: str, end_date: str) -> list:
    """新浪财经 API 获取K线"""
    # 转换代码格式: 600519 -> sh600519, 000001 -> sz000001
    if code.startswith("6") or code.startswith("5"):
        scode = f"sh{code}"
    else:
        scode = f"sz{code}"

    # 计算需要的数据量（大约）
    url = f"https://quotes.sina.cn/cn/api/jsonp_v2.php/var%20_{scode}=/CN_MarketDataService.getKLineData?symbol={scode}&scale=240&ma=no&datalen=500"

    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE

    req = urllib.request.Request(url, headers={
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
        "Referer": "https://finance.sina.com.cn/",
    })

    with urllib.request.urlopen(req, timeout=10, context=ctx) as resp:
        content = resp.read().decode("utf-8")

    # 解析 JSONP: var _sh600519=([...]);
    match = re.search(r'\[.*\]', content, re.DOTALL)
    if not match:
        return []

    data = json.loads(match.group())
    if not data:
        return []

    result = []
    prev_close = None
    for item in data:
        date_str = item.get("day", "")
        if not date_str:
            continue

        # 过滤日期范围
        if date_str < start_date or date_str > end_date:
            continue

        open_price = float(item.get("open", 0))
        high_price = float(item.get("high", 0))
        low_price = float(item.get("low", 0))
        close_price = float(item.get("close", 0))
        volume = float(item.get("volume", 0))

        # 计算涨跌幅
        pct_change = 0
        if prev_close and prev_close > 0:
            pct_change = (close_price - prev_close) / prev_close * 100
        prev_close = close_price

        result.append({
            "date": date_str,
            "open": open_price,
            "close": close_price,
            "high": high_price,
            "low": low_price,
            "volume": volume,
            "amount": 0,  # 新浪财经不直接提供成交额
            "pct_change": round(pct_change, 2),
        })
    return result


def fetch_with_eastmoney(code: str, start_date: str, end_date: str) -> list:
    """东方财富 API 获取K线（fallback）"""
    # 转换代码格式: 600519 -> 1.600519 (上海), 000001 -> 0.000001 (深圳)
    if code.startswith("6") or code.startswith("5"):
        secid = f"1.{code}"
    else:
        secid = f"0.{code}"

    # 日期格式转换: 2025-01-01 -> 20250101
    start = start_date.replace("-", "")
    end = end_date.replace("-", "")

    url = f"https://push2his.eastmoney.com/api/qt/stock/kline/get?secid={secid}&fields1=f1,f2,f3,f4,f5,f6&fields2=f51,f52,f53,f54,f55,f56,f57,f58,f59,f60,f61&klt=101&fqt=1&beg={start}&end={end}"

    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE

    req = urllib.request.Request(url, headers={
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
        "Referer": "https://quote.eastmoney.com/",
    })

    with urllib.request.urlopen(req, timeout=10, context=ctx) as resp:
        data = json.loads(resp.read().decode("utf-8"))

    if data.get("rc") != 0 or not data.get("data"):
        return []

    klines = data["data"].get("klines", [])
    if not klines:
        return []

    result = []
    for line in klines:
        # 格式: 日期,开盘,收盘,最高,最低,成交量,成交额,振幅,涨跌幅,涨跌额,换手率
        parts = line.split(",")
        if len(parts) >= 7:
            result.append({
                "date": parts[0],
                "open": float(parts[1]),
                "close": float(parts[2]),
                "high": float(parts[3]),
                "low": float(parts[4]),
                "volume": float(parts[5]),
                "amount": float(parts[6]),
                "pct_change": float(parts[8]) if len(parts) > 8 else 0,
            })
    return result


def main():
    if len(sys.argv) < 4:
        print(json.dumps({"error": "usage: fetch_kline.py <code> <start_date> <end_date>"}))
        sys.exit(1)

    code = sys.argv[1]
    start_date = sys.argv[2]
    end_date = sys.argv[3]

    # 优先新浪财经，失败则东方财富
    try:
        records = fetch_with_sina(code, start_date, end_date)
        if records:
            print(json.dumps(records, ensure_ascii=False))
            return
    except Exception:
        pass

    # Fallback to Eastmoney API
    try:
        records = fetch_with_eastmoney(code, start_date, end_date)
        if records:
            print(json.dumps(records, ensure_ascii=False))
            return
    except Exception as e:
        print(json.dumps({"error": str(e)}))
        sys.exit(1)

    print(json.dumps([]))


if __name__ == "__main__":
    main()
