"""数据服务

统一通过「数据源层」(datasources) 获取数据：
  适配器取原始数据 -> standardizer 统一口径 -> router 自动降级
保证不论数据来自东财/腾讯/新浪/AKShare，入库口径一致（volume=股, amount=元）。

对外方法签名与返回字段保持兼容，避免破坏现有路由与前端。
"""
import akshare as ak
import pandas as pd
from typing import List, Dict, Optional
from datetime import date, datetime, timedelta
import logging
import json
import urllib.request
import ssl
import threading
import time

# 数据源层（统一路由 + 标准化）
from datasources import data_router, standards as std

logger = logging.getLogger(__name__)

# BaoStock 不是线程安全的，需要加锁
_baostock_lock = threading.Lock()

# 行情缓存（120秒有效期）
_quote_cache = {}
_quote_cache_time = 0
_QUOTE_CACHE_TTL = 120  # 秒
_cache_refreshing = False  # 后台刷新标志

# 通用数据缓存（涨跌家数、板块概览、K线等）
_data_cache = {}
_DATA_CACHE_TTL = {}  # 每个key独立的TTL


_TENCENT_QUOTE_URL = "https://qt.gtimg.cn/q={tencent_code}"
_TENCENT_KLINE_URL = (
    "https://web.ifzq.gtimg.cn/appstock/app/fqkline/get?param={tencent_code},{period},"
    "{start_date},{end_date},{count},qfq"
)


# ---------------------------------------------------------------------------
# 兼容层：把「标准口味的记录」转成前端/现有路由熟悉的字段名
# ---------------------------------------------------------------------------
def kline_to_legacy(rec: Dict) -> Dict:
    """标准K线 -> 旧字段名（date/prev_close/change_pct），volume 保留为标准「股」口径"""
    return {
        "date": rec.get("trade_date"),
        "open": rec.get("open"),
        "close": rec.get("close"),
        "high": rec.get("high"),
        "low": rec.get("low"),
        "volume": rec.get("volume"),          # 标准口径：股
        "amount": rec.get("amount"),          # 标准口径：元
        "pct_change": rec.get("pct_change"),
        "turnover": rec.get("turnover"),
    }


def quote_to_legacy(rec: Dict, symbol: str) -> Dict:
    """标准行情 -> 旧字段名（prev_close/change_pct/pe_ratio/pb_ratio）"""
    return {
        "symbol": symbol,
        "name": rec.get("name"),
        "price": rec.get("price"),
        "open": rec.get("open"),
        "high": rec.get("high"),
        "low": rec.get("low"),
        "prev_close": rec.get("pre_close"),
        "change": rec.get("change"),
        "change_pct": rec.get("pct_change"),
        "volume": rec.get("volume"),
        "amount": rec.get("amount"),
        "turnover": rec.get("turnover"),
        "pe_ratio": rec.get("pe"),
        "pb_ratio": rec.get("pb"),
        "quote_time": rec.get("quote_time"),
        "source": rec.get("source"),
    }


def _to_tencent_code(symbol: str) -> str:
    """将 588000.SH / 600519.SH / 000001.SZ 转为 sh588000 / sz000001"""
    parts = symbol.split(".")
    code = parts[0]
    suffix = parts[1].upper() if len(parts) > 1 else ""
    if suffix == "SH":
        return f"sh{code}"
    elif suffix == "SZ":
        return f"sz{code}"
    # 默认按代码前缀推断
    if code.startswith("6") or code.startswith("5"):
        return f"sh{code}"
    return f"sz{code}"


def _to_baostock_code(symbol: str) -> str:
    """将 600519.SH 转为 sh.600519"""
    parts = symbol.split(".")
    code = parts[0]
    suffix = parts[1].upper() if len(parts) > 1 else ""
    if suffix == "SH":
        return f"sh.{code}"
    elif suffix == "SZ":
        return f"sz.{code}"
    if code.startswith("6") or code.startswith("5"):
        return f"sh.{code}"
    return f"sz.{code}"


def _parse_tencent_quote(raw: str, symbol: str) -> Optional[Dict]:
    """解析腾讯行情数据"""
    # v_sh600519="1~贵州茅台~600519~1800.00~1795.00~1798.00~..."
    lines = raw.strip().split("\n")
    for line in lines:
        if "=" not in line:
            continue
        var_name, var_value = line.split("=", 1)
        var_value = var_value.strip('";')
        fields = var_value.split("~")
        if len(fields) < 45:
            continue
        try:
            return {
                "symbol": symbol,
                "name": fields[1],
                "price": float(fields[3]) if fields[3] else 0,
                "change": float(fields[31]) if fields[31] else 0,
                "change_pct": float(fields[32]) if fields[32] else 0,
                "open": float(fields[5]) if fields[5] else 0,
                "high": float(fields[33]) if fields[33] else 0,
                "low": float(fields[34]) if fields[34] else 0,
                "prev_close": float(fields[4]) if fields[4] else 0,
                "volume": float(fields[6]) if fields[6] else 0,
                "amount": float(fields[37]) if fields[37] else 0,
                "turnover": float(fields[38]) if fields[38] else None,
                "pe_ratio": float(fields[39]) if fields[39] else None,
                "pb_ratio": None,
                "market_cap": float(fields[45]) if len(fields) > 45 and fields[45] else None,
                "float_cap": float(fields[44]) if len(fields) > 44 and fields[44] else None,
                "limit_up": None,
                "limit_down": None,
            }
        except (ValueError, IndexError) as e:
            logger.warning(f"解析腾讯行情数据失败: {e}")
            continue
    return None


class DataServiceImpl:
    def __init__(self):
        # 初始化 SSL 上下文（跳过证书验证）
        self._ssl_ctx = ssl.create_default_context()
        self._ssl_ctx.check_hostname = False
        self._ssl_ctx.verify_mode = ssl.CERT_NONE

    def _latest_full_trade_date(self, db):
        """返回「覆盖最全的最近交易日」。

        不能直接用 max(trade_date)：增量同步或单标的补数会在最新日期写入
        极少量行（例如只有 3 只），若据此统计会把全市场结果算成个位数。
        这里取最近 10 个交易日中**行数最多**的那天（行数相同取更近的）。
        """
        from models.market_data import MarketData
        from sqlalchemy import func

        rows = (
            db.query(MarketData.trade_date, func.count(MarketData.id))
            .group_by(MarketData.trade_date)
            .order_by(MarketData.trade_date.desc())
            .limit(10)
            .all()
        )
        if not rows:
            return None
        return max(rows, key=lambda r: (r[1], r[0]))[0]

    def _tencent_realtime_quote(self, symbol: str) -> Optional[Dict]:
        """腾讯 API 获取实时行情（fallback）"""
        try:
            tencent_code = _to_tencent_code(symbol)
            url = _TENCENT_QUOTE_URL.format(tencent_code=tencent_code)
            req = urllib.request.Request(url, headers={
                "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
                "Referer": "https://qt.gtimg.cn/",
            })
            with urllib.request.urlopen(req, timeout=10, context=self._ssl_ctx) as resp:
                raw = resp.read().decode("gbk")
            result = _parse_tencent_quote(raw, symbol)
            if result:
                logger.info(f"腾讯 API fallback 成功: {symbol}")
            return result
        except Exception as e:
            logger.warning(f"腾讯 API fallback 失败 {symbol}: {e}")
            return None

    def _baostock_realtime_quote(self, symbol: str) -> Optional[Dict]:
        """BaoStock 获取最近交易日数据作为准实时行情（fallback）"""
        try:
            import baostock as bs
            bs_code = _to_baostock_code(symbol)
            code = symbol.split(".")[0]

            # 登录
            lg = bs.login()
            if lg.error_code != '0':
                return None

            try:
                # 获取最近5天数据
                end_date = datetime.now().strftime("%Y-%m-%d")
                start_date = (datetime.now() - timedelta(days=10)).strftime("%Y-%m-%d")

                rs = bs.query_history_k_data_plus(
                    bs_code,
                    "date,open,high,low,close,volume,amount,pctChg",
                    start_date=start_date,
                    end_date=end_date,
                    frequency="d",
                    adjustflag="2"
                )

                if rs.error_code != '0':
                    return None

                rows = []
                while rs.next():
                    rows.append(rs.get_row_data())

                if not rows:
                    return None

                # 使用最新一条数据
                latest = rows[-1]
                if len(latest) < 8:
                    return None

                close = float(latest[4]) if latest[4] else 0
                prev_close = float(rows[-2][4]) if len(rows) > 1 and rows[-2][4] else close
                change = close - prev_close
                change_pct = (change / prev_close * 100) if prev_close else 0

                return {
                    "symbol": symbol,
                    "name": code,
                    "price": close,
                    "change": change,
                    "change_pct": change_pct,
                    "open": float(latest[1]) if latest[1] else 0,
                    "high": float(latest[2]) if latest[2] else 0,
                    "low": float(latest[3]) if latest[3] else 0,
                    "prev_close": prev_close,
                    "volume": float(latest[5]) if latest[5] else 0,
                    "amount": float(latest[6]) if latest[6] else 0,
                    "turnover": None,
                    "pe_ratio": None,
                    "pb_ratio": None,
                    "market_cap": None,
                    "float_cap": None,
                    "limit_up": None,
                    "limit_down": None,
                    "is_historical": True,  # 标记为历史数据
                    "trade_date": latest[0],
                }
            finally:
                bs.logout()
        except Exception as e:
            logger.warning(f"BaoStock fallback 失败 {symbol}: {e}")
            return None

    def get_csi300_constituents_baostock(self) -> List[Dict]:
        """BaoStock 获取沪深300成分股列表"""
        try:
            import baostock as bs

            lg = bs.login()
            if lg.error_code != '0':
                logger.warning(f"BaoStock 登录失败: {lg.error_msg}")
                return []

            try:
                rs = bs.query_hs300_stocks()
                if rs.error_code != '0':
                    logger.warning(f"获取沪深300成分股失败: {rs.error_msg}")
                    return []

                result = []
                while rs.next():
                    row = rs.get_row_data()
                    # row: [updateDate, code, code_name]
                    bs_code = row[1]  # sh.600000
                    name = row[2]

                    # 转换为标准格式: sh.600000 -> 600000.SH
                    parts = bs_code.split(".")
                    code = parts[1]
                    market = parts[0].upper()
                    symbol = f"{code}.{market}"

                    result.append({
                        "symbol": symbol,
                        "name": name,
                        "update_date": row[0],
                    })

                logger.info(f"BaoStock 获取沪深300成分股成功: {len(result)} 只")
                return result
            finally:
                bs.logout()
        except Exception as e:
            logger.error(f"BaoStock 获取沪深300成分股失败: {e}")
            return []

    def get_stock_data_with_valuation_baostock(
        self, symbol: str, start_date: str, end_date: str
    ) -> List[Dict]:
        """BaoStock 获取股票日线数据（含估值字段）"""
        try:
            import baostock as bs

            bs_code = _to_baostock_code(symbol)

            lg = bs.login()
            if lg.error_code != '0':
                return []

            try:
                # 查询字段: 日期、OHLCV、成交额、换手率、估值
                fields = "date,code,open,high,low,close,volume,amount,turn,peTTM,pbMRQ,psTTM"

                rs = bs.query_history_k_data_plus(
                    bs_code,
                    fields,
                    start_date=start_date,
                    end_date=end_date,
                    frequency="d",
                    adjustflag="2"  # 前复权
                )

                if rs.error_code != '0':
                    return []

                result = []
                while rs.next():
                    row = rs.get_row_data()
                    # 跳过空数据
                    if not row[4]:  # close 为空
                        continue

                    result.append({
                        "date": row[0],
                        "symbol": symbol,
                        "open": float(row[2]) if row[2] else 0,
                        "high": float(row[3]) if row[3] else 0,
                        "low": float(row[4]) if row[4] else 0,
                        "close": float(row[5]) if row[5] else 0,
                        "volume": float(row[6]) if row[6] else 0,  # 股
                        "amount": float(row[7]) if row[7] else 0,  # 元
                        "turnover_rate": float(row[8]) if row[8] else None,  # %
                        "pe_ttm": float(row[9]) if row[9] else None,
                        "pb_mrq": float(row[10]) if row[10] else None,
                        "ps_ttm": float(row[11]) if row[11] else None,
                    })

                return result
            finally:
                bs.logout()
        except Exception as e:
            logger.warning(f"BaoStock 获取股票数据失败 {symbol}: {e}")
            return []

    def _baostock_kline_data(
        self, symbol: str, start_date: str, end_date: str, period: str = "daily", adjust: str = "qfq"
    ) -> List[Dict]:
        """BaoStock 获取K线数据（fallback），支持5年数据"""
        with _baostock_lock:
            try:
                import baostock as bs
                bs.login()
                bs_code = _to_baostock_code(symbol)
                freq_map = {"daily": "d", "weekly": "w", "monthly": "m"}
                freq = freq_map.get(period, "d")
                adjust_map = {"qfq": "2", "hfq": "1", "": "3"}
                adjustflag = adjust_map.get(adjust, "2")
                fields = "date,open,high,low,close,volume,amount"
                rs = bs.query_history_k_data_plus(
                    bs_code, fields,
                    start_date=start_date, end_date=end_date,
                    frequency=freq, adjustflag=adjustflag
                )
                result = []
                while rs.error_code == "0" and rs.next():
                    row = rs.get_row_data()
                    if len(row) >= 6 and row[4]:  # close is not empty
                        result.append({
                            "date": row[0],
                            "open": float(row[1]) if row[1] else 0,
                            "high": float(row[2]) if row[2] else 0,
                            "low": float(row[3]) if row[3] else 0,
                            "close": float(row[4]) if row[4] else 0,
                            "volume": float(row[5]) if row[5] else 0,
                            "amount": float(row[6]) if len(row) > 6 and row[6] else None,
                            "pct_change": None,
                        })
                bs.logout()
                if result:
                    logger.info(f"BaoStock K线 fallback 成功: {symbol} {freq}, {len(result)} 条")
                return result
            except Exception as e:
                logger.warning(f"BaoStock K线 fallback 失败 {symbol}: {e}")
                return []

    def _tencent_kline_data(
        self, symbol: str, start_date: str, end_date: str, period: str = "daily"
    ) -> List[Dict]:
        """腾讯 API 获取K线数据（fallback），支持 daily/weekly/monthly"""
        try:
            tencent_code = _to_tencent_code(symbol)
            # 日期格式转换: 20250101 -> 2025-01-01 (如果还没有连字符)
            if "-" not in start_date and len(start_date) == 8:
                start = f"{start_date[:4]}-{start_date[4:6]}-{start_date[6:]}"
            else:
                start = start_date
            if "-" not in end_date and len(end_date) == 8:
                end = f"{end_date[:4]}-{end_date[4:6]}-{end_date[6:]}"
            else:
                end = end_date
            # 映射 period 到腾讯 API 参数
            period_map = {"daily": "day", "weekly": "week", "monthly": "month"}
            tc_period = period_map.get(period, "day")
            # 5年数据量：日K约1250条，周K约260条，月K约60条
            count_map = {"daily": 1500, "weekly": 300, "monthly": 80}
            count = count_map.get(period, 1500)
            url = _TENCENT_KLINE_URL.format(
                tencent_code=tencent_code, period=tc_period,
                start_date=start, end_date=end, count=count
            )
            req = urllib.request.Request(url, headers={
                "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
                "Referer": "https://web.ifzq.gtimg.cn/",
            })
            with urllib.request.urlopen(req, timeout=15, context=self._ssl_ctx) as resp:
                data = json.loads(resp.read().decode("utf-8"))

            if data.get("code") not in (0, None) and not data.get("data"):
                return []

            stock_data = data.get("data", {})
            if not stock_data:
                return []

            stock_key = next(iter(stock_data), None)
            if not stock_key:
                return []

            # 腾讯 API 返回的 key 根据 period 不同而不同
            qfq_key = f"qfq{tc_period}"
            klines = stock_data[stock_key].get(qfq_key) or stock_data[stock_key].get(tc_period)
            if not klines:
                return []

            result = []
            for k in klines:
                if len(k) >= 6:
                    result.append({
                        "date": k[0],
                        "open": float(k[1]),
                        "close": float(k[2]),
                        "high": float(k[3]),
                        "low": float(k[4]),
                        "volume": float(k[5]),
                        "amount": None,
                        "pct_change": None,
                    })
            if result:
                logger.info(f"腾讯 API K线 fallback 成功: {symbol} {tc_period}, {len(result)} 条")
            return result
        except Exception as e:
            logger.warning(f"腾讯 API K线 fallback 失败 {symbol}: {e}")
            return []

    # ========== 实时行情 ==========
    def _tencent_batch_quotes(self, symbols: List[str]) -> Dict[str, Dict]:
        """腾讯批量行情 API，一次 HTTP 请求获取多只股票"""
        if not symbols:
            return {}
        try:
            codes = [_to_tencent_code(s) for s in symbols]
            url = f"https://qt.gtimg.cn/q={','.join(codes)}"
            req = urllib.request.Request(url, headers={
                "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
                "Referer": "https://qt.gtimg.cn/",
            })
            with urllib.request.urlopen(req, timeout=10, context=self._ssl_ctx) as resp:
                raw = resp.read().decode("gbk")

            result = {}
            lines = [l for l in raw.strip().split("\n") if "=" in l]
            for i, line in enumerate(lines):
                if i >= len(symbols):
                    break
                symbol = symbols[i]
                parsed = _parse_tencent_quote(line, symbol)
                if parsed:
                    result[symbol] = parsed
            return result
        except Exception as e:
            logger.warning(f"腾讯批量行情失败: {e}")
            return {}

    def _refresh_quote_cache(self, symbols: Optional[List[str]] = None):
        """刷新行情缓存。
        symbols 不为空时：只增量刷新指定股票（经数据源路由，东财->腾讯->新浪 自动降级）
        symbols 为空时：全量刷新（数据源路由 all_spot）
        """
        global _quote_cache, _quote_cache_time
        now = time.time()

        if symbols:
            # 增量刷新：**只跳过缓存里已有且未过期的标的**，其余必须实际去取。
            # 注意：不能因为"缓存未过期"就整体提前返回——否则请求一只缓存中
            # 不存在的股票（如未在持仓/自选中的个股）时会取不到数据而误判为不存在。
            cache_fresh = (now - _quote_cache_time < _QUOTE_CACHE_TTL and _quote_cache)
            missing = []
            for s in symbols:
                code = s.split(".")[0]
                if cache_fresh and code in _quote_cache:
                    continue
                missing.append(s)
            if not missing:
                return

            res = data_router.get_quote(missing)
            for rec in res["data"]:
                code = (rec.get("symbol") or "").split(".")[0]
                if not code:
                    continue
                legacy = quote_to_legacy(rec, rec.get("symbol"))
                legacy["source"] = res["source"]
                _quote_cache[code] = legacy
            if res["data"]:
                _quote_cache_time = now
                logger.debug(f"行情缓存增量刷新: {len(res['data'])} 只, 源={res['source']}")
            return

        # 全量刷新：经路由层 all_spot（未过期则直接复用）
        if now - _quote_cache_time < _QUOTE_CACHE_TTL and _quote_cache:
            return
        try:
            res = data_router.get_all_spot()
            new_cache = {}
            for rec in res["data"]:
                code = (rec.get("symbol") or "").split(".")[0]
                if not code:
                    continue
                legacy = quote_to_legacy(rec, rec.get("symbol"))
                legacy["source"] = res["source"]
                new_cache[code] = legacy
            if new_cache:
                _quote_cache = new_cache
                _quote_cache_time = now
                logger.info(f"行情缓存已刷新，共 {len(new_cache)} 只, 源={res['source']}")
        except Exception as e:
            logger.warning(f"刷新行情缓存失败: {e}")

    def get_realtime_quote(self, symbol: str) -> Optional[Dict]:
        """获取单只股票实时行情（带缓存，经数据源路由自动降级）"""
        code = symbol.split(".")[0]
        now = time.time()
        # 缓存命中且未过期
        if code in _quote_cache and now - _quote_cache_time < _QUOTE_CACHE_TTL:
            result = _quote_cache[code].copy()
            result["symbol"] = symbol
            return result
        # 缓存过期或没有，增量刷新这只股票
        self._refresh_quote_cache(symbols=[symbol])
        if code in _quote_cache:
            result = _quote_cache[code].copy()
            result["symbol"] = symbol
            return result
        return None

    def get_batch_quotes(self, symbols: List[str]) -> Dict[str, Dict]:
        """批量获取多只股票行情（经数据源路由，一次请求多只）。

        返回字典的 key **一律使用调用方传入的 symbol 原样**（而不是数据源
        规范化后的代码）。否则当入参缺少交易所后缀（如持仓里的 '600570'）
        时，首次冷请求会以 '600570.SH' 为键，调用方用 '600570' 取不到，
        导致该标的被漏算（典型表现：首页总资产首次偏小、刷新后又正常）。
        """
        global _quote_cache_time
        now = time.time()
        cache_valid = now - _quote_cache_time < _QUOTE_CACHE_TTL and _quote_cache

        result = {}
        missing = []

        # 先从缓存取
        for symbol in symbols:
            code = symbol.split(".")[0]
            if cache_valid and code in _quote_cache:
                quote = _quote_cache[code].copy()
                quote["symbol"] = symbol
                result[symbol] = quote
            else:
                missing.append(symbol)

        # 缓存没有的，经路由层批量获取
        if missing:
            res = data_router.get_quote(missing)
            # 数据源返回的 symbol 可能是规范化后的（带后缀）。建立
            # 「纯代码 -> 调用方原始 symbol」映射，保证用原始 symbol 作返回键。
            by_code = {}
            for symbol in missing:
                by_code.setdefault(symbol.split(".")[0], symbol)
            for rec in res["data"]:
                sym = rec.get("symbol") or ""
                code = sym.split(".")[0]
                legacy = quote_to_legacy(rec, sym)
                legacy["source"] = res["source"]
                _quote_cache[code] = legacy
                # 优先用调用方传入的原始 symbol 作键（保持与入参一致）
                out_key = by_code.get(code, sym)
                legacy_out = legacy.copy()
                legacy_out["symbol"] = out_key
                result[out_key] = legacy_out
            if res["data"]:
                _quote_cache_time = now

        return result

    def get_market_indices(self) -> List[Dict]:
        """获取主要指数行情（经数据源路由，东财主 -> 新浪兜底）"""
        res = data_router.get_indices()
        if res["data"]:
            return [{
                "name": r.get("name"),
                "value": r.get("value"),
                "change": r.get("change"),
                "change_pct": r.get("change_pct"),
            } for r in res["data"]]
        logger.warning("指数获取失败，所有数据源不可用")
        return []

    def _sina_market_indices(self) -> List[Dict]:
        """新浪财经获取主要指数（fallback）"""
        import urllib.request
        import ssl
        import json

        indices = [
            ("上证指数", "sh000001"),
            ("深证成指", "sz399001"),
            ("创业板指", "sz399006"),
            ("沪深300", "sh000300"),
            ("科创50", "sh000688"),
            ("创业板50", "sz399673"),
            ("中证500", "sh000905"),
            ("中证1000", "sh000852"),
        ]
        codes = ",".join([c for _, c in indices])
        url = f"https://hq.sinajs.cn/list={codes}"

        ctx = ssl.create_default_context()
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE

        req = urllib.request.Request(url, headers={
            "User-Agent": "Mozilla/5.0",
            "Referer": "https://finance.sina.com.cn/",
        })

        try:
            with urllib.request.urlopen(req, timeout=10, context=ctx) as resp:
                content = resp.read().decode("gbk")

            result = []
            lines = content.strip().split("\n")
            for line in lines:
                if "=" not in line:
                    continue
                var_name, data = line.split("=", 1)
                code = var_name.split("_")[-1]
                fields = data.strip('";\r').split(",")
                if len(fields) < 4:
                    continue

                name = fields[0]
                # 新浪指数格式：名称,今开,昨收,最新价,最高,最低,...
                # 最新价（收盘）在 fields[3]，fields[1] 是今日开盘价，不能当收盘价用。
                close = float(fields[3]) if fields[3] else 0
                prev_close = float(fields[2]) if fields[2] else 0
                change = close - prev_close
                change_pct = (change / prev_close * 100) if prev_close else 0

                # 匹配中文名
                display_name = name
                for cn_name, sina_code in indices:
                    if sina_code.endswith(code):
                        display_name = cn_name
                        break

                result.append({
                    "name": display_name,
                    "value": close,
                    "change": round(change, 2),
                    "change_pct": round(change_pct, 2),
                })
            return result
        except Exception as e:
            logger.error(f"新浪财经获取指数也失败: {e}")
            return []

    # ========== K线数据 ==========
    def get_kline_data(
        self,
        symbol: str,
        period: str = "daily",
        start_date: Optional[str] = None,
        end_date: Optional[str] = None,
        adjust: str = "qfq",
    ) -> List[Dict]:
        """获取K线数据（经数据源路由 + 标准化，返回标准口径）

        period: daily/weekly/monthly
        adjust: qfq(前复权)/hfq(后复权)/none(不复权)
        返回字段沿用旧名（date/pct_change），但 volume=股、amount=元 为统一标准口径。
        """
        # 先归一日期为 YYYY-MM-DD（缓存 key 必须包含日期范围，否则不同区间会串数据）
        if not end_date:
            end_date = datetime.now().strftime(std.STANDARD_DATE_FMT)
        if not start_date:
            start_date = (datetime.now() - timedelta(days=1825)).strftime(std.STANDARD_DATE_FMT)
        from datasources.standardizer import normalize_date
        start_fmt = normalize_date(start_date)
        end_fmt = normalize_date(end_date)

        cache_key = f"kline_{symbol}_{period}_{adjust}_{start_fmt}_{end_fmt}"
        cache_ttl = 300 if period == "daily" else 1800
        cached = self._get_cached(cache_key)
        if cached:
            return cached

        # 经路由层获取（自动降级 + 标准化）
        res = data_router.get_kline(symbol, start_fmt, end_fmt, period, adjust)
        if res["data"]:
            logger.debug(f"K线 {symbol} 命中数据源: {res['source']}")
            result = [kline_to_legacy(r) for r in res["data"]]
            self._set_cached(cache_key, result, cache_ttl)
            return result

        logger.error(f"K线获取失败（所有数据源均不可用）: {symbol} {res['attempts']}")
        return []

    # ========== 通用缓存 ==========
    def _get_cached(self, key: str):
        """获取缓存数据，过期返回 None"""
        if key in _data_cache:
            expire_time = _DATA_CACHE_TTL.get(key, 0)
            if time.time() < expire_time:
                return _data_cache[key]
            else:
                del _data_cache[key]
                del _DATA_CACHE_TTL[key]
        return None

    def _set_cached(self, key: str, value, ttl: int):
        """设置缓存数据"""
        _data_cache[key] = value
        _DATA_CACHE_TTL[key] = time.time() + ttl

    # ========== 市场全景 ==========
    def _fetch_industry_fund_flow(self, limit: int = 5) -> Dict:
        """东财行业板块资金流（真实主力净流入，单位=元）"""
        import json as _json
        url = ("https://push2.eastmoney.com/api/qt/clist/get?pn=1&pz=100&po=1&np=1"
               "&fltt=2&invt=2&fid=f62&fs=m:90+t:2"
               "&fields=f12,f14,f2,f3,f62,f66,f72,f78")
        req = urllib.request.Request(url, headers={
            "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7)",
            "Referer": "https://data.eastmoney.com/",
        })
        with urllib.request.urlopen(req, timeout=15, context=self._ssl_ctx) as resp:
            data = _json.loads(resp.read().decode("utf-8"))
        items = (data.get("data") or {}).get("diff") or []
        if isinstance(items, dict):
            items = list(items.values())
        if not items:
            raise ValueError("东财板块资金流返回空")

        def _num(v):
            try:
                return float(v)
            except (ValueError, TypeError):
                return None

        rows = [{
            "name": it.get("f14"),
            "change_pct": _num(it.get("f3")),
            "net_inflow": _num(it.get("f62")),      # 主力净流入（元）
            "super_inflow": _num(it.get("f66")),    # 超大单净流入
            "big_inflow": _num(it.get("f72")),      # 大单净流入
        } for it in items if _num(it.get("f62")) is not None]

        inflow_sorted = sorted(rows, key=lambda x: x["net_inflow"], reverse=True)
        return {
            "fund_inflow": inflow_sorted[:limit],
            "fund_outflow": inflow_sorted[-limit:][::-1],
        }

    def get_sector_overview(self, limit: int = 5) -> Dict:
        """A股行业板块全景：涨跌前5 + 真实资金流入流出前5 + 每板块前3股票"""
        # 检查缓存（60秒）
        cached = self._get_cached("sector_overview")
        if cached:
            return cached

        # 尝试 AKShare 获取真实板块数据
        try:
            df = ak.stock_board_industry_name_em()
            if not df.empty:
                df["涨跌幅"] = pd.to_numeric(df.get("涨跌幅", 0), errors="coerce").fillna(0)
                sorted_df = df.sort_values("涨跌幅", ascending=False)

                top_gainers = []
                top_losers = []
                for _, row in sorted_df.head(limit).iterrows():
                    top_gainers.append({
                        "name": str(row.get("板块名称", "")),
                        "change_pct": float(row.get("涨跌幅", 0)),
                    })
                for _, row in sorted_df.tail(limit).iterrows():
                    top_losers.append({
                        "name": str(row.get("板块名称", "")),
                        "change_pct": float(row.get("涨跌幅", 0)),
                    })
                top_losers.reverse()

                # 真实资金流：优先用东财主力净流入接口
                try:
                    flow = self._fetch_industry_fund_flow(limit)
                    fund_inflow = flow["fund_inflow"]
                    fund_outflow = flow["fund_outflow"]
                except Exception as e:
                    logger.warning(f"东财板块资金流失败，资金流字段置空: {e}")
                    fund_inflow = [{"name": x["name"], "change_pct": x["change_pct"], "net_inflow": None} for x in top_gainers]
                    fund_outflow = [{"name": x["name"], "change_pct": x["change_pct"], "net_inflow": None} for x in top_losers]

                result = {
                    "top_gainers": top_gainers,
                    "top_losers": top_losers,
                    "fund_inflow": fund_inflow,
                    "fund_outflow": fund_outflow,
                    "sector_stocks": {},
                }
                self._set_cached("sector_overview", result, 60)
                return result
        except Exception as e:
            logger.warning(f"AKShare 板块概览失败: {e}, 使用本地数据库")

        # 本地数据库 fallback
        from database import SessionLocal
        from models.market_data import StockInfo, MarketData
        from sqlalchemy import func

        db = SessionLocal()
        try:
            latest_date = self._latest_full_trade_date(db)
            if not latest_date:
                return {"top_gainers": [], "top_losers": [], "fund_inflow": [], "fund_outflow": [], "sector_stocks": {}}

            # 只分析沪深两市（排除北交所）
            stocks = [s for s in db.query(StockInfo).all() if std.is_tradable_a_stock(s.symbol)]
            industry_map = {}
            all_symbols = []
            for stock in stocks:
                industry = stock.industry or "未分类"
                if industry not in industry_map:
                    industry_map[industry] = []
                industry_map[industry].append(stock)
                all_symbols.append(stock.symbol)

            # 批量查询所有股票的最新行情数据（避免 N+1 查询）
            all_market_data = {}
            batch_size = 500
            for i in range(0, len(all_symbols), batch_size):
                batch_syms = all_symbols[i:i + batch_size]
                rows = db.query(MarketData).filter(
                    MarketData.symbol.in_(batch_syms),
                    MarketData.trade_date == latest_date,
                ).all()
                for md in rows:
                    all_market_data[md.symbol] = md

            sectors = []
            for industry, stock_objs in industry_map.items():
                changes = []
                stock_details = []
                for s in stock_objs[:100]:
                    md = all_market_data.get(s.symbol)
                    if md and md.pct_change is not None:
                        changes.append(float(md.pct_change))
                        stock_details.append({
                            "name": s.name or s.symbol,
                            "symbol": s.symbol,
                            "change_pct": float(md.pct_change) if md.pct_change else 0,
                            "price": float(md.close) if md.close else 0,
                        })
                if changes:
                    avg_change = sum(changes) / len(changes)
                    stock_details.sort(key=lambda x: x["change_pct"], reverse=True)
                    sectors.append({
                        "name": industry,
                        "change_pct": round(avg_change, 2),
                        "stocks": stock_details[:3],
                    })

            sectors.sort(key=lambda x: x["change_pct"], reverse=True)

            top_gainers = [{"name": s["name"], "change_pct": s["change_pct"]} for s in sectors[:limit]]
            top_losers = [{"name": s["name"], "change_pct": s["change_pct"]} for s in sectors[-limit:]]
            top_losers.reverse()

            sector_stocks = {}
            for s in sectors:
                sector_stocks[s["name"]] = s["stocks"]

            result = {
                "top_gainers": top_gainers,
                "top_losers": top_losers,
                "fund_inflow": [{"name": s["name"], "change_pct": s["change_pct"], "net_inflow": None} for s in sectors[:limit]],
                "fund_outflow": [{"name": s["name"], "change_pct": s["change_pct"], "net_inflow": None} for s in sectors[-limit:]],
                "sector_stocks": sector_stocks,
            }
            self._set_cached("sector_overview", result, 60)
            return result
        except Exception as e:
            logger.error(f"本地数据库板块概览也失败: {e}")
            return {"top_gainers": [], "top_losers": [], "fund_inflow": [], "fund_outflow": [], "sector_stocks": {}}
        finally:
            db.close()

    def get_market_breadth(self) -> Dict:
        """获取市场涨跌家数统计（本地数据库优先）"""
        # 检查缓存（60秒）
        cached = self._get_cached("market_breadth")
        if cached:
            return cached

        from database import SessionLocal
        from models.market_data import MarketData, StockInfo
        from sqlalchemy import func
        from datetime import datetime
        import requests as req

        db = SessionLocal()
        try:
            # 选取「覆盖最全的最近交易日」——不能简单用 max(trade_date)。
            # 原因：增量同步/单个标的补数会在最新日期写入极少量行（如仅 3 只），
            # 若直接按 max(date) 统计，涨跌家数会得到"全市场只有 3 只"的错误结果。
            latest_date = self._latest_full_trade_date(db)
            if not latest_date:
                raise Exception("本地数据库为空")

            # 统计涨跌家数（本地数据库）
            data = db.query(MarketData.pct_change).filter(
                MarketData.trade_date == latest_date
            ).all()

            if not data:
                raise Exception("最新交易日无数据")

            changes = [d[0] for d in data if d[0] is not None]
            up_count = sum(1 for c in changes if c > 0)
            down_count = sum(1 for c in changes if c < 0)
            flat_count = sum(1 for c in changes if c == 0)
            total = len(changes)

            # 涨停跌停统计（涨跌幅 >= 9.9% 或 <= -9.9%）
            limit_up_count = sum(1 for c in changes if c >= 9.9)
            limit_down_count = sum(1 for c in changes if c <= -9.9)

            # 涨跌幅分布（按区间统计）
            ranges = [
                {"label": "<-7%", "count": 0, "color": "#2B8A3E"},
                {"label": "-7~-5%", "count": 0, "color": "#40C057"},
                {"label": "-5~-3%", "count": 0, "color": "#63E6BE"},
                {"label": "-3~-1%", "count": 0, "color": "#96F2D7"},
                {"label": "-1~0%", "count": 0, "color": "#C3FAE8"},
                {"label": "0~1%", "count": 0, "color": "#FFC9C9"},
                {"label": "1~3%", "count": 0, "color": "#FFA8A8"},
                {"label": "3~5%", "count": 0, "color": "#FF6B6B"},
                {"label": "5~7%", "count": 0, "color": "#F03E3E"},
                {"label": ">7%", "count": 0, "color": "#E03131"},
            ]
            for c in changes:
                if c < -7: ranges[0]["count"] += 1
                elif c < -5: ranges[1]["count"] += 1
                elif c < -3: ranges[2]["count"] += 1
                elif c < -1: ranges[3]["count"] += 1
                elif c < 0: ranges[4]["count"] += 1
                elif c < 1: ranges[5]["count"] += 1
                elif c < 3: ranges[6]["count"] += 1
                elif c < 5: ranges[7]["count"] += 1
                elif c < 7: ranges[8]["count"] += 1
                else: ranges[9]["count"] += 1

            # 全市场成交额：本地库 amount 优先（已入库的交易日直接求和），
            # 缺失时再用腾讯API补齐。
            local_amount = db.query(func.sum(MarketData.amount)).filter(
                MarketData.trade_date == latest_date
            ).scalar()
            stocked_amount = db.query(func.count(MarketData.amount)).filter(
                MarketData.trade_date == latest_date,
                MarketData.amount.isnot(None),
            ).scalar() or 0

            stocks = db.query(StockInfo).all()
            tencent_syms = []
            for stock in stocks:
                code, exchange = stock.symbol.split('.')
                prefix = 'sh' if exchange == 'SH' else 'sz'
                tencent_syms.append(f'{prefix}{code}')

            # 本地 amount 覆盖率足够高（>=90%）则直接采用，避免多余的外部请求。
            if local_amount and stocked_amount >= total * 0.9:
                today_volume_yi = local_amount / 1e8
                logger.info(f"全市场成交额: {today_volume_yi:.2f} 亿元 (来自本地数据库)")
            else:
                total_amount = 0
                # 腾讯批量接口对 URL 长度敏感：2000 只会导致 HTTP 414，故用 300。
                batch_size = 300
                headers = {'User-Agent': 'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36'}
                for i in range(0, len(tencent_syms), batch_size):
                    batch = tencent_syms[i:i + batch_size]
                    try:
                        url = f'https://qt.gtimg.cn/q={",".join(batch)}'
                        r = req.get(url, headers=headers, timeout=30)
                        if r.status_code != 200:
                            continue
                        lines = [l.strip() for l in r.text.split(';') if l.strip() and '="' in l]
                        for line in lines:
                            try:
                                data_str = line.split('="')[1].rstrip('"')
                                parts = data_str.split('~')
                                if len(parts) > 37 and parts[37]:
                                    amount_wan = float(parts[37])  # 成交额(万)
                                    total_amount += amount_wan * 10000  # 转为元
                            except (ValueError, IndexError):
                                continue
                    except Exception as e:
                        logger.warning(f"腾讯API获取成交额失败 (batch {i//batch_size + 1}): {e}")

                today_volume_yi = total_amount / 1e8 if total_amount else 0
                logger.info(f"全市场成交额: {today_volume_yi:.2f} 亿元 (来自腾讯API)")

            # 预估全日成交量
            now = datetime.now()
            trade_start = now.replace(hour=9, minute=30, second=0)
            trade_end = now.replace(hour=15, minute=0, second=0)
            if now < trade_start:
                estimated_volume_yi = today_volume_yi
            elif now >= trade_end:
                estimated_volume_yi = today_volume_yi
            else:
                elapsed = (now - trade_start).total_seconds() / 3600
                total_hours = 5.5
                ratio = min(elapsed / total_hours, 1.0)
                estimated_volume_yi = today_volume_yi / ratio if ratio > 0 else today_volume_yi

            result = {
                "up_count": up_count,
                "down_count": down_count,
                "flat_count": flat_count,
                "limit_up_count": limit_up_count,
                "limit_down_count": limit_down_count,
                "total": total,
                "up_ratio": round(up_count / total * 100, 2) if total > 0 else 0,
                "down_ratio": round(down_count / total * 100, 2) if total > 0 else 0,
                "trade_date": str(latest_date),
                "coverage": round(total / 5000 * 100, 1),
                "coverage_hint": f"基于本地数据库 {total} 只股票统计",
                "ranges": ranges,
                "today_volume_yi": round(today_volume_yi, 2),
                "estimated_volume_yi": round(estimated_volume_yi, 2),
            }
            self._set_cached("market_breadth", result, 60)
            return result
        except Exception as e:
            logger.warning(f"本地数据库涨跌统计失败: {e}, 尝试实时 API")
            return self._get_market_breadth_realtime()
        finally:
            db.close()

    def _get_market_breadth_realtime(self) -> Dict:
        """实时 API 获取涨跌家数"""
        try:
            df = ak.stock_zh_a_spot_em()
            up_count = len(df[df["涨跌幅"] > 0])
            down_count = len(df[df["涨跌幅"] < 0])
            flat_count = len(df[df["涨跌幅"] == 0])
            total = len(df)

            return {
                "up_count": up_count,
                "down_count": down_count,
                "flat_count": flat_count,
                "total": total,
                "up_ratio": round(up_count / total * 100, 2) if total > 0 else 0,
                "down_ratio": round(down_count / total * 100, 2) if total > 0 else 0,
                "trade_date": datetime.now().strftime("%Y-%m-%d"),
                "coverage": 100.0,
                "coverage_hint": "全市场实时统计",
            }
        except Exception as e:
            logger.error(f"实时涨跌统计失败: {e}")
            return {
                "up_count": 0,
                "down_count": 0,
                "flat_count": 0,
                "total": 0,
                "up_ratio": 0,
                "down_ratio": 0,
                "trade_date": datetime.now().strftime("%Y-%m-%d"),
                "coverage": 0,
                "error": str(e),
            }

    # ========== 数据同步 ==========
    def get_all_a_stocks_eastmoney(self) -> List[Dict]:
        """获取全部 A 股股票列表（经数据源路由，东财主 -> AKShare 兜底）。

        说明：本项目只分析沪深两市，**北交所标的直接过滤掉**（不做分析）。
        """
        res = data_router.get_stock_list()
        if not res["data"]:
            logger.error(f"获取 A 股列表失败（所有源不可用）: {res['attempts']}")
            return []
        result = []
        skipped_bj = 0
        for r in res["data"]:
            symbol = r.get("symbol")
            # 去掉北交所（4xxxxx / 8xxxxx / 920xxx）
            if not std.is_tradable_a_stock(symbol):
                skipped_bj += 1
                continue
            result.append({
                "symbol": symbol,
                "name": r.get("name", ""),
                "market": r.get("market", "A股"),
                "exchange": std.symbol_exchange(symbol),
                "industry": r.get("industry", ""),
                "list_date": r.get("list_date") or "",
                "is_st": bool(r.get("is_st", 0)),
            })
        logger.info(f"获取 A 股列表成功: {len(result)} 只, 源={res['source']}, 已过滤北交所 {skipped_bj} 只")
        return result

    def full_sync_stock_info(self, stocks: List[Dict]) -> int:
        """全量同步股票基本信息到本地数据库"""
        from database import SessionLocal
        from models.market_data import StockInfo

        db = SessionLocal()
        added = 0
        try:
            for s in stocks:
                existing = db.query(StockInfo).filter(StockInfo.symbol == s["symbol"]).first()
                if not existing:
                    info = StockInfo(
                        symbol=s["symbol"],
                        name=s["name"],
                        market=s.get("market", "A股"),
                        industry=s.get("industry", ""),
                        list_date=s.get("list_date") or None,
                        is_st=s.get("is_st", False),
                    )
                    db.add(info)
                    added += 1
            db.commit()
            logger.info(f"股票信息同步完成: 新增 {added} 条")
        except Exception as e:
            logger.error(f"同步股票信息失败: {e}")
            db.rollback()
        finally:
            db.close()
        return added

    def full_sync_kline(self, symbols: List[str], days: int = 1825) -> Dict:
        """全量同步K线数据到本地（经数据源路由 + 标准化，落库口径统一）

        入库字段：open/high/low/close/volume(股)/amount(元)/turnover/pct_change
        """
        from database import SessionLocal
        from models.market_data import MarketData

        start_date = (datetime.now() - timedelta(days=days)).strftime(std.STANDARD_DATE_FMT)
        end_date = datetime.now().strftime(std.STANDARD_DATE_FMT)

        success = 0
        failed = 0
        records = 0
        src_used = {}

        db = SessionLocal()
        try:
            for symbol in symbols:
                try:
                    # 经路由层获取标准K线（自动降级 + 标准化）
                    res = data_router.get_kline(symbol, start_date, end_date, "daily", "qfq")
                    data = res["data"]
                    if not data:
                        failed += 1
                        continue
                    src_used[res["source"]] = src_used.get(res["source"], 0) + 1

                    from services.market_writer import PRICE_FIELDS, upsert_daily_bars
                    rows_out = [{"trade_date": row.get("trade_date"),
                                 **{f: row.get(f) for f in PRICE_FIELDS}} for row in data]
                    st = upsert_daily_bars(db, symbol, rows_out)
                    db.commit()
                    records += st["added"] + st["updated"]
                    success += 1
                except Exception as e:
                    logger.warning(f"同步 {symbol} K线失败: {e}")
                    failed += 1
                    db.rollback()
        finally:
            db.close()

        logger.info(f"全量同步完成: 成功{success} 失败{failed} 记录{records} 数据源分布={src_used}")
        return {"success": success, "failed": failed, "records": records, "sources": src_used}


    # ========== 搜索 ==========
    def search_stock(self, keyword: str) -> List[Dict]:
        """按关键字搜索股票（本地数据库优先）"""
        from database import SessionLocal
        from models.market_data import StockInfo

        db = SessionLocal()
        try:
            # 本地搜索：排除北交所标的
            results = [s for s in db.query(StockInfo).filter(
                (StockInfo.name.contains(keyword)) |
                (StockInfo.symbol.contains(keyword))
            ).limit(40).all() if std.is_tradable_a_stock(s.symbol)][:20]
            return [
                {
                    "symbol": s.symbol,
                    "name": s.name,
                    "market": s.market or "A股",
                    "industry": s.industry or "",
                }
                for s in results
            ]
        except Exception as e:
            logger.warning(f"本地搜索失败: {e}, 尝试 AKShare")
            try:
                df = ak.stock_info_a_code_name()
                mask = df["name"].str.contains(keyword, na=False) | df["code"].str.contains(keyword, na=False)
                matched = df[mask].head(20)
                results = []
                for _, row in matched.iterrows():
                    code = str(row["code"]).zfill(6)
                    symbol = f"{code}.SH" if code.startswith("6") else f"{code}.SZ"
                    results.append({"symbol": symbol, "name": str(row["name"])})
                return results
            except Exception as e2:
                logger.error(f"AKShare 搜索也失败: {e2}")
                return []
        finally:
            db.close()

    # ========== 行业板块资金流向 ==========
    def get_industry_sectors(self) -> List[Dict]:
        """获取行业板块资金流向"""
        try:
            df = ak.stock_board_industry_name_em()
            result = []
            for _, row in df.head(30).iterrows():
                result.append({
                    "name": str(row.get("板块名称", "")),
                    "change_pct": float(row.get("涨跌幅", 0)),
                    "stock_count": int(row.get("家数", 0)),
                    "leader": str(row.get("领涨股票", "")),
                    "leader_change_pct": float(row.get("领涨股票-涨跌幅", 0)) if pd.notna(row.get("领涨股票-涨跌幅")) else 0,
                    "total_amount": float(row.get("总成交额", 0)) if pd.notna(row.get("总成交额")) else 0,
                    "net_inflow": float(row.get("净流入", 0)) if pd.notna(row.get("净流入")) else 0,
                })
            return result
        except Exception as e:
            logger.warning(f"AKShare 行业板块失败: {e}, 尝试新浪 API fallback")
            result = self._eastmoney_industry_sectors()
            if result:
                return result
            logger.warning("新浪行业板块返回空, 使用本地数据库")
            return self._local_industry_sectors()

    def _eastmoney_industry_sectors(self) -> List[Dict]:
        """新浪财经 API 获取行业板块（fallback）"""
        import urllib.request, ssl, json, re
        # 新浪行业板块接口
        url = (
            "https://vip.stock.finance.sina.com.cn/quotes_service/api/json_v2.php/"
            "Market_Center.getHQNodeData?page=1&num=40&sort=changepercent&asc=0"
            "&node=sina_industry"
        )
        ctx = ssl.create_default_context()
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
        req = urllib.request.Request(url, headers={
            "User-Agent": "Mozilla/5.0",
            "Referer": "https://finance.sina.com.cn/",
        })
        try:
            with urllib.request.urlopen(req, timeout=10, context=ctx) as resp:
                content = resp.read().decode("gbk")
            # 新浪返回 JSON 数组
            data = json.loads(content)
            if not data:
                return []
            result = []
            for item in data:
                result.append({
                    "name": item.get("name", ""),
                    "change_pct": float(item.get("changepercent", 0)),
                    "stock_count": int(item.get("count", 0)),
                    "leader": item.get("leadstock", ""),
                    "leader_change_pct": float(item.get("leadstockchangepercent", 0)),
                    "total_amount": float(item.get("amount", 0)),
                    "net_inflow": 0,
                })
            return result
        except Exception as e:
            logger.warning(f"新浪行业板块失败: {e}")
            return []

    def _local_industry_sectors(self) -> List[Dict]:
        """本地数据库统计行业板块（最终 fallback）"""
        from database import SessionLocal
        from models.market_data import StockInfo, MarketData
        from sqlalchemy import func

        db = SessionLocal()
        try:
            latest_date = self._latest_full_trade_date(db)
            if not latest_date:
                return []
            # 只分析沪深两市（排除北交所）
            stocks = [s for s in db.query(StockInfo).all() if std.is_tradable_a_stock(s.symbol)]
            if not stocks:
                return []

            industry_map = {}
            for stock in stocks:
                industry = stock.industry or "未分类"
                if industry not in industry_map:
                    industry_map[industry] = []
                industry_map[industry].append(stock.symbol)

            result = []
            for industry, symbols in industry_map.items():
                changes = []
                for sym in symbols[:50]:
                    md = db.query(MarketData).filter(
                        MarketData.symbol == sym,
                        MarketData.trade_date == latest_date,
                    ).first()
                    if md and md.pct_change is not None:
                        changes.append(float(md.pct_change))
                if changes:
                    avg_change = sum(changes) / len(changes)
                    result.append({
                        "name": industry,
                        "change_pct": round(avg_change, 2),
                        "stock_count": len(symbols),
                        "leader": "",
                        "leader_change_pct": 0,
                        "total_amount": 0,
                        "net_inflow": 0,
                    })
            result.sort(key=lambda x: x["change_pct"], reverse=True)
            return result[:30]
        except Exception as e:
            logger.error(f"本地数据库行业板块也失败: {e}")
            return []
        finally:
            db.close()

    # ========== 美股概览 ==========
    def get_us_sector_overview(self) -> Dict:
        """美股概览：主要指数(新浪API) + 涨跌幅前5 + 跌幅前5"""
        import requests as req

        # 用新浪财经API获取美股三大指数
        indices = []
        try:
            headers = {"User-Agent": "Mozilla/5.0", "Referer": "https://finance.sina.com.cn"}
            url = "https://hq.sinajs.cn/list=int_dji,int_nasdaq,int_sp500"
            r = req.get(url, headers=headers, timeout=10)
            # 解析: var hq_str_int_dji="道琼斯,46247.29,299.97,0.65";
            for line in r.text.strip().split("\n"):
                if '="' not in line:
                    continue
                try:
                    data_str = line.split('="')[1].rstrip('";')
                    parts = data_str.split(",")
                    if len(parts) >= 4:
                        indices.append({
                            "name": parts[0],
                            "price": float(parts[1]),
                            "change_pct": float(parts[3]),
                        })
                except (ValueError, IndexError):
                    continue
        except Exception as e:
            logger.warning(f"新浪API获取美股指数失败: {e}")

        if not indices:
            indices = [
                {"name": "道琼斯", "price": 0, "change_pct": 0},
                {"name": "纳斯达克", "price": 0, "change_pct": 0},
                {"name": "标普500", "price": 0, "change_pct": 0},
            ]

        # 尝试AKShare获取个股数据，失败则返回空
        try:
            df = ak.stock_us_spot_em()
            if df.empty:
                return {"indices": indices, "top_gainers": [], "top_losers": [], "hot_stocks": []}

            df["涨跌幅"] = pd.to_numeric(df.get("涨跌幅", 0), errors="coerce").fillna(0)
            df["最新价"] = pd.to_numeric(df.get("最新价", 0), errors="coerce").fillna(0)
            df["成交额"] = pd.to_numeric(df.get("成交额", 0), errors="coerce").fillna(0)

            sorted_up = df.sort_values("涨跌幅", ascending=False).head(10)
            sorted_down = df.sort_values("涨跌幅", ascending=True).head(10)
            sorted_hot = df.sort_values("成交额", ascending=False).head(10)

            def _us_row(row):
                return {
                    "name": str(row.get("名称", "")),
                    "symbol": str(row.get("代码", "")),
                    "price": float(row.get("最新价", 0)),
                    "change_pct": float(row.get("涨跌幅", 0)),
                    "amount": float(row.get("成交额", 0)),
                }

            return {
                "indices": indices,
                "top_gainers": [_us_row(r) for _, r in sorted_up.iterrows()],
                "top_losers": [_us_row(r) for _, r in sorted_down.iterrows()],
                "hot_stocks": [_us_row(r) for _, r in sorted_hot.iterrows()],
            }
        except Exception as e:
            logger.warning(f"AKShare 美股个股失败: {e}")
            return {
                "indices": indices,
                "top_gainers": [],
                "top_losers": [],
                "hot_stocks": [],
            }

    # ========== 条件选股 ==========
    def scan_stocks_by_condition(self, condition: str) -> List[Dict]:
        """根据中文描述全市场扫描选股"""
        from database import SessionLocal
        from models.market_data import StockInfo, MarketData
        from sqlalchemy import func
        import requests as req

        db = SessionLocal()
        try:
            # 获取覆盖最全的最近交易日
            latest_date = self._latest_full_trade_date(db)
            if not latest_date:
                return []

            # 批量获取本地数据（一次查询代替逐只查询）
            all_market_data = db.query(MarketData).filter(
                MarketData.trade_date == latest_date
            ).all()
            md_map = {md.symbol: md for md in all_market_data}

            # 只分析沪深两市（排除北交所 4xxxxx/8xxxxx/920xxx）
            stocks = [s for s in db.query(StockInfo).all() if std.is_tradable_a_stock(s.symbol)]

            # 构建腾讯API批量查询列表
            tencent_syms = []
            symbol_list = []
            for stock in stocks:
                if stock.symbol not in md_map:
                    continue
                code, exchange = stock.symbol.split('.')
                prefix = 'sh' if exchange == 'SH' else 'sz'
                tencent_syms.append(f'{prefix}{code}')
                symbol_list.append(stock.symbol)

            # 通过腾讯API批量获取实时行情（换手率、市盈率、总市值、成交额）
            realtime_data = {}
            batch_size = 500
            headers = {'User-Agent': 'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36'}
            for i in range(0, len(tencent_syms), batch_size):
                batch = tencent_syms[i:i + batch_size]
                batch_symbols = symbol_list[i:i + batch_size]
                try:
                    url = f'https://qt.gtimg.cn/q={",".join(batch)}'
                    r = req.get(url, headers=headers, timeout=30)
                    if r.status_code != 200:
                        continue
                    lines = [l.strip() for l in r.text.split(';') if l.strip() and '=\"' in l]
                    for idx, line in enumerate(lines):
                        if idx >= len(batch_symbols):
                            break
                        symbol = batch_symbols[idx]
                        try:
                            data_str = line.split('="')[1].rstrip('"')
                            parts = data_str.split('~')
                            if len(parts) < 46:
                                continue
                            # parts[37]=成交额(万), parts[38]=换手率(%), parts[39]=市盈率, parts[44]=总市值(亿), parts[45]=流通市值(亿)
                            amount_wan = float(parts[37]) if parts[37] else 0
                            turnover_rate = float(parts[38]) if parts[38] else None
                            pe_ratio = float(parts[39]) if parts[39] else None
                            total_mv_yi = float(parts[44]) if parts[44] else None
                            realtime_data[symbol] = {
                                "turnover_rate": turnover_rate,
                                "pe_ratio": pe_ratio,
                                "total_mv": total_mv_yi * 100000000 if total_mv_yi else None,  # 亿转元
                                "amount": amount_wan * 10000 if amount_wan else 0,  # 万转元
                            }
                        except (ValueError, IndexError):
                            continue
                except Exception as e:
                    logger.warning(f"腾讯API批量获取失败 (batch {i//batch_size + 1}): {e}")

            logger.info(f"获取全市场实时行情成功，共 {len(realtime_data)} 只股票")

            results = []
            import re as _re
            # 多个条件用 + 连接，逻辑为「且」：所有子条件须同时满足
            sub_conditions = [c.strip() for c in _re.split(r'[+、,，\s]+', condition.strip()) if c.strip()]

            # 技术指标辅助计算函数
            def _ema(data, n):
                """计算 EMA"""
                if not data:
                    return []
                result = [0.0] * len(data)
                k = 2 / (n + 1)
                result[0] = data[0]
                for i in range(1, len(data)):
                    result[i] = data[i] * k + result[i - 1] * (1 - k)
                return result

            def _calc_macd(closes):
                """计算 MACD: DIF, DEA, MACD柱"""
                if len(closes) < 26:
                    return None, None, None
                ema12 = _ema(closes, 12)
                ema26 = _ema(closes, 26)
                dif = [ema12[i] - ema26[i] for i in range(len(closes))]
                dea = _ema(dif, 9)
                macd_bar = [(dif[i] - dea[i]) * 2 for i in range(len(closes))]
                return dif, dea, macd_bar

            def _calc_kdj(closes, highs, lows, n=9):
                """计算 KDJ"""
                if len(closes) < n:
                    return None, None, None
                k_vals = [50.0] * len(closes)
                d_vals = [50.0] * len(closes)
                for i in range(n - 1, len(closes)):
                    window_high = max(highs[i - n + 1:i + 1])
                    window_low = min(lows[i - n + 1:i + 1])
                    if window_high == window_low:
                        rsv = 50.0
                    else:
                        rsv = (closes[i] - window_low) / (window_high - window_low) * 100
                    if i == n - 1:
                        k_vals[i] = 2 / 3 * 50 + 1 / 3 * rsv
                        d_vals[i] = 2 / 3 * 50 + 1 / 3 * k_vals[i]
                    else:
                        k_vals[i] = 2 / 3 * k_vals[i - 1] + 1 / 3 * rsv
                        d_vals[i] = 2 / 3 * d_vals[i - 1] + 1 / 3 * k_vals[i]
                j_vals = [3 * k_vals[i] - 2 * d_vals[i] for i in range(len(closes))]
                return k_vals, d_vals, j_vals

            def _calc_ma(closes, n):
                """计算简单移动平均线 MA"""
                if len(closes) < n:
                    return None
                ma = [sum(closes[i - n + 1:i + 1]) / n for i in range(n - 1, len(closes))]
                return ma

            def _get_kline_closes(symbol, period):
                """获取指定周期的K线收盘价序列"""
                try:
                    klines = self.get_kline_data(symbol=symbol, period=period)
                    if not klines or len(klines) < 26:
                        return None, None, None
                    closes = [float(k["close"]) for k in klines if k.get("close")]
                    highs = [float(k["high"]) for k in klines if k.get("high")]
                    lows = [float(k["low"]) for k in klines if k.get("low")]
                    return closes, highs, lows
                except Exception:
                    return None, None, None

            # 技术指标缓存（避免重复计算）
            tech_cache = {}

            for stock in stocks:
                md = md_map.get(stock.symbol)
                if not md:
                    continue

                # 优先从实时数据获取，否则用本地数据
                rt = realtime_data.get(stock.symbol, {})
                info = {
                    "symbol": stock.symbol,
                    "name": stock.name or "",
                    "price": md.close or 0,
                    "pct_change": float(md.pct_change) if md.pct_change else 0,
                    "amount": rt.get("amount") or (float(md.amount) if md.amount else 0),
                    "volume": float(md.volume) if md.volume else 0,
                    "turnover_rate": rt.get("turnover_rate") or (float(md.turnover) if md.turnover else None),
                    "pe_ratio": rt.get("pe_ratio"),
                    "total_mv": rt.get("total_mv"),
                }

                def matches(sub):
                    s = sub.strip()
                    if not s:
                        return True
                    sl = s.lower()
                    # 组合词：放量/缩量 + 涨跌（同时满足）
                    if "放量" in s or "缩量" in s:
                        vol = info["volume"] or 0
                        vol_ok = vol > 100000000 if "放量" in s else vol < 100000000
                        if "涨停" in s or "大涨" in s or "上涨" in s:
                            return vol_ok and info["pct_change"] > 0
                        if "跌停" in s or "大跌" in s or "下跌" in s:
                            return vol_ok and info["pct_change"] < 0
                        return vol_ok
                    # 涨跌停 / 大涨大跌 / 上涨下跌
                    if "涨停" in s:
                        return info["pct_change"] >= 9.5
                    if "跌停" in s:
                        return info["pct_change"] <= -9.5
                    if "大涨" in s:
                        return info["pct_change"] >= 5
                    if "大跌" in s:
                        return info["pct_change"] <= -5
                    if "上涨" in s:
                        return info["pct_change"] > 0
                    if "下跌" in s:
                        return info["pct_change"] < 0
                    # 高价 / 低价
                    if "高价" in s:
                        return info["price"] > 100
                    if "低价" in s:
                        return 0 < info["price"] < 10
                    # 成交活跃
                    if "成交活跃" in s:
                        return bool(info["amount"] and info["amount"] > 1000000000)
                    # ST 股票
                    if "st" in sl:
                        return bool(stock.is_st)
                    # 换手率
                    if "换手率" in s and info["turnover_rate"]:
                        m = _re.search(r'(\d+\.?\d*)', s)
                        if m:
                            thr = float(m.group(1))
                            if "大于" in s or ">" in s:
                                return info["turnover_rate"] > thr
                            if "小于" in s or "<" in s:
                                return info["turnover_rate"] < thr
                        return False
                    # 市值
                    if "市值" in s and info["total_mv"]:
                        m = _re.search(r'(\d+\.?\d*)', s)
                        if m:
                            thr = float(m.group(1)) * 100000000  # 亿转元
                            if "大于" in s or ">" in s:
                                return info["total_mv"] > thr
                            if "小于" in s or "<" in s:
                                return info["total_mv"] < thr
                        return False
                    # PE
                    if "pe" in sl and info["pe_ratio"]:
                        m = _re.search(r'(\d+\.?\d*)', s)
                        if m:
                            thr = float(m.group(1))
                            if "小于" in s or "<" in s:
                                return info["pe_ratio"] < thr
                            if "大于" in s or ">" in s:
                                return info["pe_ratio"] > thr
                        return False
                    # 涨幅 / 跌幅
                    if "涨幅" in s:
                        m = _re.search(r'(\d+\.?\d*)', s)
                        if m:
                            return info["pct_change"] > float(m.group(1))
                        return info["pct_change"] > 0
                    if "跌幅" in s:
                        m = _re.search(r'(\d+\.?\d*)', s)
                        if m:
                            return info["pct_change"] < -float(m.group(1))
                        return info["pct_change"] < 0
                    # 技术指标：日线/周线/月线 MACD>0, MACD金叉, KDJ金叉
                    if "macd" in sl or "kdj" in sl:
                        # 确定周期
                        period = "daily"
                        if "周线" in s or "周" in s:
                            period = "weekly"
                        elif "月线" in s or "月" in s:
                            period = "monthly"

                        # 从缓存获取或计算
                        cache_key = f"{stock.symbol}_{period}"
                        if cache_key not in tech_cache:
                            closes, highs, lows = _get_kline_closes(stock.symbol, period)
                            if closes:
                                dif, dea, macd_bar = _calc_macd(closes)
                                k_vals, d_vals, j_vals = _calc_kdj(closes, highs, lows)
                                tech_cache[cache_key] = {
                                    "closes": closes,
                                    "dif": dif, "dea": dea, "macd": macd_bar,
                                    "k": k_vals, "d": d_vals, "j": j_vals
                                }
                            else:
                                tech_cache[cache_key] = None

                        tc = tech_cache[cache_key]
                        if not tc:
                            return False

                        # MACD>0 判断（最新一天 DIF > 0）
                        if "macd" in sl and ">0" in s:
                            return tc["dif"][-1] > 0 if tc["dif"] else False

                        # MACD金叉：DIF 上穿 DEA（前一天 DIF<DEA，当天 DIF>=DEA）
                        if "macd" in sl and "金叉" in s:
                            if tc["dif"] and len(tc["dif"]) >= 2:
                                return tc["dif"][-2] < tc["dea"][-2] and tc["dif"][-1] >= tc["dea"][-1]
                            return False

                        # KDJ金叉：K 上穿 D（前一天 K<D，当天 K>=D）
                        if "kdj" in sl and "金叉" in s:
                            if tc["k"] and len(tc["k"]) >= 2:
                                return tc["k"][-2] < tc["d"][-2] and tc["k"][-1] >= tc["d"][-1]
                            return False

                        # 多头排列：MA5 > MA10 > MA20 > MA30
                        if "多头排列" in s:
                            closes = tc.get("closes")
                            if not closes:
                                return False
                            ma5 = _calc_ma(closes, 5)
                            ma10 = _calc_ma(closes, 10)
                            ma20 = _calc_ma(closes, 20)
                            ma30 = _calc_ma(closes, 30)
                            if not all([ma5, ma10, ma20, ma30]):
                                return False
                            # 取最后一个值判断
                            return ma5[-1] > ma10[-1] > ma20[-1] > ma30[-1]

                    # 行业关键词
                    if stock.industry:
                        for kw in ["科技", "新能源", "消费", "医药", "金融", "半导体", "芯片", "ai", "光伏", "锂电", "白酒"]:
                            if kw in sl and kw in stock.industry.lower():
                                return True
                    # 默认：涨幅 > 0
                    return info["pct_change"] > 0

                if all(matches(c) for c in sub_conditions):
                    results.append(info)

            # 按涨跌幅排序，返回前50
            results.sort(key=lambda x: x["pct_change"], reverse=True)
            return results[:50]
        except Exception as e:
            logger.error(f"条件选股失败: {e}")
            return []
        finally:
            db.close()

    # ========== 技术指标 ==========
    def get_technical_indicators(self, symbol: str, period: str = "daily") -> Dict:
        """计算技术指标：MA/MACD/KDJ/RSI/BOLL + 综合信号"""
        # 优先从实时API获取K线数据（AKShare + 腾讯 fallback）
        klines = self.get_kline_data(symbol=symbol, period=period)

        if not klines:
            # fallback: 尝试本地数据库
            from database import SessionLocal
            from models.market_data import MarketData
            db = SessionLocal()
            try:
                rows = db.query(MarketData).filter(
                    MarketData.symbol == symbol
                ).order_by(MarketData.trade_date.asc()).all()
                if rows:
                    klines = [{
                        "date": str(r.trade_date),
                        "close": float(r.close),
                        "open": float(r.open or 0),
                        "high": float(r.high or 0),
                        "low": float(r.low or 0),
                        "volume": float(r.volume or 0),
                    } for r in rows if r.close]
            finally:
                db.close()

        if not klines:
            return {"error": f"无 {symbol} 的K线数据"}

        closes = [float(k["close"]) for k in klines if k.get("close")]
        dates = [str(k["date"]) for k in klines if k.get("close")]

        if len(closes) < 5:
            return {"error": "数据不足，无法计算技术指标"}

        try:

            # MA 均线
            def _ma(data, n):
                result = [None] * len(data)
                for i in range(n - 1, len(data)):
                    result[i] = round(sum(data[i - n + 1:i + 1]) / n, 2)
                return result

            ma5 = _ma(closes, 5)
            ma10 = _ma(closes, 10)
            ma20 = _ma(closes, 20)
            ma60 = _ma(closes, 60) if len(closes) >= 60 else [None] * len(closes)

            # MACD (12, 26, 9)
            def _ema(data, n):
                result = [0.0] * len(data)
                k = 2 / (n + 1)
                result[0] = data[0]
                for i in range(1, len(data)):
                    result[i] = data[i] * k + result[i - 1] * (1 - k)
                return result

            ema12 = _ema(closes, 12)
            ema26 = _ema(closes, 26)
            dif = [round(ema12[i] - ema26[i], 4) for i in range(len(closes))]
            dea = _ema(dif, 9)
            dea = [round(d, 4) for d in dea]
            macd_bar = [round((dif[i] - dea[i]) * 2, 4) for i in range(len(closes))]

            # KDJ (9, 3, 3)
            n_kdj = 9
            k_vals = [50.0] * len(closes)
            d_vals = [50.0] * len(closes)
            j_vals = [50.0] * len(closes)
            for i in range(n_kdj - 1, len(closes)):
                window_high = max(closes[i - n_kdj + 1:i + 1])
                window_low = min(closes[i - n_kdj + 1:i + 1])
                if window_high == window_low:
                    rsv = 50.0
                else:
                    rsv = (closes[i] - window_low) / (window_high - window_low) * 100
                if i == n_kdj - 1:
                    k_vals[i] = round(2 / 3 * 50 + 1 / 3 * rsv, 2)
                    d_vals[i] = round(2 / 3 * 50 + 1 / 3 * k_vals[i], 2)
                else:
                    k_vals[i] = round(2 / 3 * k_vals[i - 1] + 1 / 3 * rsv, 2)
                    d_vals[i] = round(2 / 3 * d_vals[i - 1] + 1 / 3 * k_vals[i], 2)
                j_vals[i] = round(3 * k_vals[i] - 2 * d_vals[i], 2)

            # RSI (6, 12, 24)
            def _rsi(data, n):
                result = [None] * len(data)
                gains = []
                losses = []
                for i in range(1, len(data)):
                    diff = data[i] - data[i - 1]
                    gains.append(max(diff, 0))
                    losses.append(max(-diff, 0))
                if len(gains) < n:
                    return result
                avg_gain = sum(gains[:n]) / n
                avg_loss = sum(losses[:n]) / n
                if avg_loss == 0:
                    result[n] = 100.0
                else:
                    result[n] = round(100 - 100 / (1 + avg_gain / avg_loss), 2)
                for i in range(n, len(gains)):
                    avg_gain = (avg_gain * (n - 1) + gains[i]) / n
                    avg_loss = (avg_loss * (n - 1) + losses[i]) / n
                    if avg_loss == 0:
                        result[i + 1] = 100.0
                    else:
                        result[i + 1] = round(100 - 100 / (1 + avg_gain / avg_loss), 2)
                return result

            rsi6 = _rsi(closes, 6)
            rsi12 = _rsi(closes, 12)
            rsi24 = _rsi(closes, 24)

            # BOLL (20, 2)
            boll_mid = _ma(closes, 20)
            boll_upper = [None] * len(closes)
            boll_lower = [None] * len(closes)
            for i in range(19, len(closes)):
                window = closes[i - 19:i + 1]
                std = (sum((x - boll_mid[i]) ** 2 for x in window) / 20) ** 0.5
                boll_upper[i] = round(boll_mid[i] + 2 * std, 2)
                boll_lower[i] = round(boll_mid[i] - 2 * std, 2)

            # 信号判断
            last = len(closes) - 1
            signals = {}

            # MA 信号
            if ma5[last] and ma10[last] and ma20[last]:
                if ma5[last] > ma10[last] > ma20[last]:
                    signals["ma"] = {"signal": "多头排列", "desc": "MA5>MA10>MA20，均线呈多头排列"}
                elif ma5[last] < ma10[last] < ma20[last]:
                    signals["ma"] = {"signal": "空头排列", "desc": "MA5<MA10<MA20，均线呈空头排列"}
                else:
                    signals["ma"] = {"signal": "交叉整理", "desc": "均线交织，趋势不明朗"}
            else:
                signals["ma"] = {"signal": "数据不足", "desc": "MA指标数据不足"}

            # MACD 信号
            if last >= 2 and dif[last] > dea[last] and dif[last - 1] <= dea[last - 1]:
                signals["macd"] = {"signal": "金叉", "desc": "DIF上穿DEA，形成金叉买入信号"}
            elif last >= 2 and dif[last] < dea[last] and dif[last - 1] >= dea[last - 1]:
                signals["macd"] = {"signal": "死叉", "desc": "DIF下穿DEA，形成死叉卖出信号"}
            elif dif[last] > 0 and dea[last] > 0:
                signals["macd"] = {"signal": "多头", "desc": "DIF和DEA均在零轴上方，多头趋势"}
            else:
                signals["macd"] = {"signal": "空头", "desc": "DIF和DEA均在零轴下方，空头趋势"}

            # KDJ 信号
            if k_vals[last] > 80 and d_vals[last] > 80:
                signals["kdj"] = {"signal": "超买", "desc": f"KDJ值({k_vals[last]:.0f})大于80，处于超买区域"}
            elif k_vals[last] < 20 and d_vals[last] < 20:
                signals["kdj"] = {"signal": "超卖", "desc": f"KDJ值({k_vals[last]:.0f})小于20，处于超卖区域"}
            else:
                signals["kdj"] = {"signal": "中性", "desc": f"KDJ值({k_vals[last]:.0f})，处于正常区间"}

            # RSI 信号
            rsi_val = rsi6[last] if rsi6[last] else 50
            if rsi_val > 80:
                signals["rsi"] = {"signal": "超买", "desc": f"RSI6={rsi_val:.0f}，超过80，短期超买"}
            elif rsi_val > 50:
                signals["rsi"] = {"signal": "偏多", "desc": f"RSI6={rsi_val:.0f}，大于50，短期偏多"}
            elif rsi_val < 20:
                signals["rsi"] = {"signal": "超卖", "desc": f"RSI6={rsi_val:.0f}，低于20，短期超卖"}
            else:
                signals["rsi"] = {"signal": "偏空", "desc": f"RSI6={rsi_val:.0f}，低于50，短期偏空"}

            # 综合信号
            bullish = sum(1 for k in ["ma", "macd", "kdj", "rsi"] if "多" in signals.get(k, {}).get("signal", "") or "金叉" in signals.get(k, {}).get("signal", "") or "超卖" in signals.get(k, {}).get("signal", ""))
            bearish = sum(1 for k in ["ma", "macd", "kdj", "rsi"] if "空" in signals.get(k, {}).get("signal", "") or "死叉" in signals.get(k, {}).get("signal", "") or "超买" in signals.get(k, {}).get("signal", ""))
            if bullish > bearish:
                signals["overall"] = {"signal": "看多", "desc": f"综合多项技术指标，当前偏多头趋势（{bullish}多 vs {bearish}空）"}
            elif bearish > bullish:
                signals["overall"] = {"signal": "看空", "desc": f"综合多项技术指标，当前偏空头趋势（{bullish}多 vs {bearish}空）"}
            else:
                signals["overall"] = {"signal": "中性", "desc": f"技术指标多空分歧（{bullish}多 vs {bearish}空），建议观望"}

            return {
                "dates": dates,
                "closes": closes,
                "ma5": ma5,
                "ma10": ma10,
                "ma20": ma20,
                "ma60": ma60,
                "boll": {"mid": boll_mid, "upper": boll_upper, "lower": boll_lower},
                "macd": {"dif": dif, "dea": dea, "macd_bar": macd_bar},
                "kdj": {"k": k_vals, "d": d_vals, "j": j_vals},
                "rsi": {"rsi6": rsi6, "rsi12": rsi12, "rsi24": rsi24},
                "signals": signals,
            }
        except Exception as e:
            logger.error(f"技术指标计算失败 {symbol}: {e}")
            return {"error": str(e)}


data_service = DataServiceImpl()
