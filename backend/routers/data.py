"""数据中心 API"""
from fastapi import APIRouter, Depends
from fastapi.responses import StreamingResponse
from sqlalchemy.orm import Session
from sqlalchemy import func, text
from typing import List, Dict
import math
from datetime import datetime, timedelta
from database import get_db, engine
from models.position import Position, Account
from models.market_data import MarketData
from services.data_service import data_service
from schemas import DashboardResponse, DataSyncResponse

router = APIRouter(prefix="/api/data", tags=["数据中心"])


def _load_price_series(db: Session, positions, days: int):
    """加载每个持仓最近 days 个交易日的收盘价序列，本地缺失时用数据源补取。

    返回 (price_series, all_dates)：
      price_series: symbol -> {date_str: close}
      all_dates:    所有出现过的交易日期集合
    """
    price_series = {}
    all_dates = set()
    for pos in positions:
        rows = (
            db.query(MarketData.trade_date, MarketData.close)
            .filter(MarketData.symbol == pos.symbol)
            .order_by(MarketData.trade_date.desc())
            .limit(days)
            .all()
        )
        series = {}
        for d, c in rows:
            if c is None:
                continue
            ds = d.strftime("%Y-%m-%d")
            series[ds] = float(c)
            all_dates.add(ds)
        # 本地无数据时，从数据源路由补取真实K线
        if not series:
            try:
                klines = data_service.get_kline_data(pos.symbol, "daily")
                for k in klines[-days:]:
                    series[k["date"]] = float(k["close"])
                    all_dates.add(k["date"])
            except Exception:
                pass
        price_series[pos.symbol] = series
    return price_series, all_dates


def _build_nav_series(price_series, all_dates, positions, available_cash: float, days: int):
    """按交易日构建连续净值序列，缺失日期沿用最近收盘价。

    返回 [(date_str, value), ...]，date_str 为 YYYY-MM-DD。
    """
    sorted_dates = sorted(all_dates)[-days:]
    nav = []
    last_close = {}  # symbol -> 最近一个可用收盘价
    for ds in sorted_dates:
        total = available_cash
        for pos in positions:
            series = price_series.get(pos.symbol, {})
            close = series.get(ds)
            if close is not None:
                last_close[pos.symbol] = close
            else:
                # 当日无行情（停牌、数据源缺口、本地K线未更新）时沿用最近收盘价，
                # 直接跳过会把该持仓市值整块丢掉，曲线末端会出现断崖式下折
                close = last_close.get(pos.symbol)
            if close is not None:
                total += pos.quantity * close
        nav.append((ds, round(max(total, 0), 2)))
    return nav


def _build_real_asset_curve(db: Session, positions, available_cash: float, days: int = 30):
    """基于持仓的真实历史收盘价构建资产净值曲线（首页 30 日曲线）。"""
    if not positions:
        return []
    price_series, all_dates = _load_price_series(db, positions, days)
    if not all_dates:
        return []
    nav = _build_nav_series(price_series, all_dates, positions, available_cash, days)
    return [{"date": ds[5:], "value": value} for ds, value in nav]


def _build_pnl_calendar(db: Session, positions, available_cash: float, years: int = 5):
    """构建日/月/年收益日历。

    每日收益 = 当日净值 - 前一日净值，口径与首页净值曲线一致（纯市值变动）。
    随后按自然月 / 自然年聚合。
    """
    if not positions:
        return {"daily": [], "monthly": [], "yearly": []}
    days = max(years, 1) * 252
    price_series, all_dates = _load_price_series(db, positions, days)
    if not all_dates:
        return {"daily": [], "monthly": [], "yearly": []}
    nav = _build_nav_series(price_series, all_dates, positions, available_cash, days)

    daily = []
    monthly_map = {}
    yearly_map = {}
    prev_value = None
    for ds, value in nav:
        if prev_value is not None:
            pnl = round(value - prev_value, 2)
            daily.append({"date": ds, "pnl": pnl})
            month_key = ds[:7]
            monthly_map[month_key] = monthly_map.get(month_key, 0.0) + pnl
            year_key = ds[:4]
            yearly_map[year_key] = yearly_map.get(year_key, 0.0) + pnl
        prev_value = value

    monthly = [{"period": k, "pnl": round(v, 2)} for k, v in monthly_map.items()]
    yearly = [{"period": k, "pnl": round(v, 2)} for k, v in yearly_map.items()]
    return {"daily": daily, "monthly": monthly, "yearly": yearly}


@router.get("/dashboard", response_model=DashboardResponse)
def get_dashboard(account_id: int = 1, db: Session = Depends(get_db)):
    """获取首页仪表盘数据"""
    # 获取账户
    account = db.query(Account).filter(Account.id == account_id).first()
    if not account:
        # 创建默认账户
        account = Account(
            id=account_id,
            name="默认账户",
            account_type="simulated",
            initial_capital=100000,
            current_capital=100000,
            risk_preference="balanced",
        )
        db.add(account)
        db.commit()
        db.refresh(account)

    # 获取持仓
    positions = db.query(Position).filter(Position.account_id == account_id).all()

    # 批量获取行情（高性能）
    total_position_value = 0
    total_cost_basis = 0  # 持仓成本总额
    today_pnl = 0  # 今日盈亏
    if positions:
        symbols = [pos.symbol for pos in positions]
        quotes = data_service.get_batch_quotes(symbols)

        # 更新实时价格
        for pos in positions:
            quote = quotes.get(pos.symbol)
            if quote:
                pos.current_price = quote["price"]
                pos.market_value = pos.quantity * pos.current_price
                pos.profit_loss = (pos.current_price - pos.avg_cost) * pos.quantity
                pos.profit_loss_pct = (
                    (pos.current_price / pos.avg_cost - 1) * 100
                    if pos.avg_cost > 0
                    else 0
                )
                total_position_value += pos.market_value
                total_cost_basis += pos.avg_cost * pos.quantity

                # 今日盈亏 = (现价 - 昨收) × 持仓数量
                prev_close = quote.get("prev_close", 0)
                if prev_close and prev_close > 0:
                    today_pnl += (pos.current_price - prev_close) * pos.quantity

        # 计算仓位
        for pos in positions:
            pos.position_pct = (
                (pos.market_value / total_position_value * 100)
                if total_position_value > 0
                else 0
            )

        db.commit()

    # 计算 KPI
    # 可用资金 = 初始资本 - 持仓成本（不低于0，模拟盘不追踪实际扣款）
    available_cash = max(0, account.initial_capital - total_cost_basis)
    total_assets = total_position_value + available_cash
    today_pnl_pct = (today_pnl / total_cost_basis * 100) if total_cost_basis > 0 else 0
    # 累计收益率 = 总盈亏 / 持仓成本（基于实际投入资金计算）
    total_pnl = sum(p.profit_loss for p in positions)
    total_return_pct = (total_pnl / total_cost_basis * 100) if total_cost_basis > 0 else 0

    # 获取指数
    indices = data_service.get_market_indices()

    # 生成资产净值曲线（最近30个交易日）
    # 基于持仓的真实历史收盘价：每日市值 = Σ(持仓数量 × 当日收盘价) + 现金
    asset_curve = _build_real_asset_curve(db, positions, available_cash)

    # 资产分布（基于真实持仓 + 现金）
    allocation = []
    for pos in positions:
        if pos.market_value and pos.market_value > 0:
            label = pos.name or pos.symbol
            allocation.append({
                "name": label,
                "pct": round(pos.market_value / total_assets * 100, 1) if total_assets > 0 else 0,
                "amount": round(pos.market_value),
            })
    if available_cash > 0:
        allocation.append({
            "name": "现金",
            "pct": round(available_cash / total_assets * 100, 1) if total_assets > 0 else 0,
            "amount": round(available_cash),
        })

    return {
        "total_assets": total_assets,
        "today_pnl": today_pnl,
        "today_pnl_pct": today_pnl_pct,
        "position_value": total_position_value,
        "available_cash": available_cash,
        "total_return_pct": total_return_pct,
        "positions": positions,
        "indices": indices,
        "asset_curve": asset_curve,
        "allocation": allocation,
    }


@router.get("/pnl-calendar")
def get_pnl_calendar(account_id: int = 1, years: int = 5, db: Session = Depends(get_db)):
    """收益日历：日/月/年收益金额。

    每日收益 = 当日净值 - 前一日净值（纯市值变动），与首页净值曲线口径一致。
    """
    account = db.query(Account).filter(Account.id == account_id).first()
    initial_capital = account.initial_capital if account else 100000
    positions = db.query(Position).filter(Position.account_id == account_id).all()
    if not positions:
        return {"daily": [], "monthly": [], "yearly": []}
    total_cost_basis = sum((p.avg_cost or 0) * (p.quantity or 0) for p in positions)
    available_cash = max(0, initial_capital - total_cost_basis)
    return _build_pnl_calendar(db, positions, available_cash, years)


@router.post("/sync/quotes")
def sync_realtime_quotes(db: Session = Depends(get_db)):
    """同步所有持仓的实时行情"""
    positions = db.query(Position).all()
    updated = []
    for pos in positions:
        quote = data_service.get_realtime_quote(pos.symbol)
        if quote:
            pos.current_price = quote["price"]
            pos.market_value = pos.quantity * pos.current_price
            pos.profit_loss = (pos.current_price - pos.avg_cost) * pos.quantity
            pos.profit_loss_pct = (
                (pos.current_price / pos.avg_cost - 1) * 100
                if pos.avg_cost > 0
                else 0
            )
            updated.append(pos.symbol)
    db.commit()
    return DataSyncResponse(
        status="success",
        message=f"已更新 {len(updated)} 只股票行情",
        count=len(updated),
        symbols=updated,
    )


@router.post("/sync/kline/{symbol}")
def sync_kline_data(symbol: str, period: str = "daily", days: int = 1825, db: Session = Depends(get_db)):
    """同步K线数据到本地"""
    from datetime import datetime, timedelta
    start_date = (datetime.now() - timedelta(days=days)).strftime("%Y%m%d")
    end_date = datetime.now().strftime("%Y%m%d")

    klines = data_service.get_kline_data(symbol, period, start_date, end_date)

    # 统一走写入器：历史日=终态不覆盖；当日盘中快照会被后续更新覆盖，收盘后自动定格
    from services.market_writer import PRICE_FIELDS, upsert_daily_bars
    rows = [{"trade_date": k.get("trade_date") or k.get("date"),
             **{f: k.get(f) for f in PRICE_FIELDS}} for k in klines]
    st = upsert_daily_bars(db, symbol, rows, commit=True)
    count = st["added"] + st["updated"]
    return DataSyncResponse(
        status="success",
        message=(f"新增 {st['added']} 条、更新 {st['updated']} 条"
                 f"（已定格跳过 {st['skipped']} 条）"),
        count=count,
        symbols=[symbol],
    )


@router.get("/stats")
def get_data_stats(db: Session = Depends(get_db)):
    """获取本地数据统计：各表记录数、文件大小、更新时间"""
    import os
    from pathlib import Path
    from datetime import datetime

    stats = []

    # 1. market_data 表统计
    total_records = db.query(func.count(MarketData.id)).scalar() or 0

    # 标的清单取自 stock_info，**不要**对 market_data 做 SELECT DISTINCT symbol：
    # 该语句在 600 万行上无法利用 (symbol, trade_date) 索引跳过重复，实测 30.7s，
    # 而按 symbol 逐只查询是走索引区间，每只 1~6ms。
    from models.market_data import StockInfo
    symbols_list = [s for (s,) in db.query(StockInfo.symbol).order_by(StockInfo.symbol).all()]
    symbol_stats = []
    for sym in symbols_list[:50]:
        c, mn, mx = (
            db.query(func.count(MarketData.id),
                     func.min(MarketData.trade_date),
                     func.max(MarketData.trade_date))
            .filter(MarketData.symbol == sym)
            .one()
        )
        symbol_stats.append({
            "symbol": sym, "records": int(c or 0),
            "latest_date": str(mx) if mx else None,
            "earliest_date": str(mn) if mn else None,
        })

    stats.append({
        "table": "market_data",
        "label": "K线行情数据",
        "total_records": total_records,
        "symbols_count": len(symbols_list),
        "symbols": symbol_stats,
    })

    # 2. stock_info 表统计
    try:
        stock_count = db.execute(text("SELECT COUNT(*) FROM stock_info")).scalar() or 0
    except Exception:
        stock_count = 0
    stats.append({
        "table": "stock_info",
        "label": "股票基本信息",
        "total_records": stock_count,
        "symbols_count": stock_count,
        "symbols": [],
    })

    # 3. 数据库文件大小
    db_path = Path(engine.url.database) if engine.url.database else None
    db_size = 0
    db_file = ""
    if db_path and db_path.exists():
        db_size = db_path.stat().st_size
        db_file = str(db_path)
    stats.append({
        "table": "database_file",
        "label": "数据库文件",
        "file_path": db_file,
        "file_size": db_size,
        "file_size_human": _human_size(db_size),
        "total_records": 0,
        "symbols_count": 0,
        "symbols": [],
    })

    # 4. 各表记录汇总
    tables_info = []
    for table_name in ["positions", "trades", "strategies", "paper_accounts", "paper_orders", "paper_positions"]:
        try:
            cnt = db.execute(text(f"SELECT COUNT(*) FROM {table_name}")).scalar() or 0
        except Exception:
            cnt = 0
        tables_info.append({"table": table_name, "records": cnt})

    return {
        "stats": stats,
        "tables": tables_info,
        "total_market_records": total_records,
    }


@router.post("/full-sync")
def full_sync_data(days: int = 1825, db: Session = Depends(get_db)):
    """全量更新：获取全部A股股票列表 + 批量同步K线到本地数据库"""
    import threading

    # 1. 获取全A股股票列表
    stocks = data_service.get_all_a_stocks_eastmoney()
    if not stocks:
        return {"status": "error", "message": "无法获取A股股票列表，请检查网络"}

    # 2. 同步股票基本信息到 stock_info
    info_added = data_service.full_sync_stock_info(stocks)

    # 3. 批量同步K线（全部股票）
    symbols_to_sync = [s["symbol"] for s in stocks]

    result = data_service.full_sync_kline(symbols_to_sync, days=days)

    return {
        "status": "success",
        "message": f"全量更新完成：获取 {len(stocks)} 只股票，同步 {result['success']} 只K线，共 {result['records']} 条记录",
        "stock_list_count": len(stocks),
        "stock_info_added": info_added,
        "kline_synced": result["success"],
        "kline_failed": result["failed"],
        "kline_records": result["records"],
    }


@router.post("/full-sync-stream")
def full_sync_stream(db: Session = Depends(get_db)):
    """全量更新（流式进度版）：使用 SSE 推送进度，优化版"""
    from fastapi.responses import StreamingResponse
    import json as json_mod
    import time
    from concurrent.futures import ThreadPoolExecutor, as_completed

    def generate():
        # Step 1: 获取股票列表
        yield f"data: {json_mod.dumps({'step': 'stock_list', 'status': 'start'})}\n\n"
        stocks = data_service.get_all_a_stocks_eastmoney()
        if not stocks:
            yield f"data: {json_mod.dumps({'step': 'stock_list', 'status': 'error', 'message': '获取股票列表失败'})}\n\n"
            return
        yield f"data: {json_mod.dumps({'step': 'stock_list', 'status': 'done', 'count': len(stocks)})}\n\n"

        # Step 2: 同步股票基本信息
        yield f"data: {json_mod.dumps({'step': 'stock_info', 'status': 'start'})}\n\n"
        info_added = data_service.full_sync_stock_info(stocks)
        yield f"data: {json_mod.dumps({'step': 'stock_info', 'status': 'done', 'added': info_added})}\n\n"

        # Step 3: 筛选需要更新的股票（跳过7天内有数据的）
        yield f"data: {json_mod.dumps({'step': 'filter', 'status': 'start'})}\n\n"
        from models.market_data import MarketData
        from database import SessionLocal as _SessionLocal
        from datetime import timedelta
        import subprocess, sys, json as _json

        db_local = _SessionLocal()
        cutoff_date = (datetime.now() - timedelta(days=7)).date()
        
        all_symbols = [s["symbol"] for s in stocks]
        symbols_to_sync = []
        
        for symbol in all_symbols:
            latest = db_local.query(MarketData).filter(
                MarketData.symbol == symbol,
                MarketData.trade_date >= cutoff_date
            ).first()
            if not latest:
                symbols_to_sync.append(symbol)
        
        yield f"data: {json_mod.dumps({'step': 'filter', 'status': 'done', 'total': len(all_symbols), 'to_sync': len(symbols_to_sync), 'skipped': len(all_symbols) - len(symbols_to_sync)})}\n\n"

        if not symbols_to_sync:
            db_local.close()
            yield f"data: {json_mod.dumps({'step': 'kline', 'status': 'done', 'success': 0, 'failed': 0, 'records': 0, 'message': '所有股票数据已是最新'})}\n\n"
            # 仍同步 Data Bridge（数据可能已有更新）
            try:
                from datasources import bridge as _bridge
                exp = _bridge.export_watchlist_and_positions()
                env = _bridge.sync_source_order()
                yield f"data: {json_mod.dumps({'step': 'bridge', 'status': 'done', 'exported': exp['exported'], 'source_order': env['order']})}\n\n"
            except Exception as e:
                yield f"data: {json_mod.dumps({'step': 'bridge', 'status': 'error', 'message': str(e)[:120]})}\n\n"
            # 同步一份到 Sequoia-X
            try:
                from services import sequoia_sync
                res = sequoia_sync.sync_recent(days=30)
                yield f"data: {json_mod.dumps({'step': 'sequoia', 'status': 'done', 'synced': res.get('synced', 0)})}\n\n"
            except Exception as e:
                yield f"data: {json_mod.dumps({'step': 'sequoia', 'status': 'error', 'message': str(e)[:120]})}\n\n"
            yield f"data: {json_mod.dumps({'step': 'all_done', 'stock_count': len(stocks), 'info_added': info_added, 'kline_success': 0, 'kline_records': 0})}\n\n"
            return

        # Step 4: 批量同步K线（并行处理）
        yield f"data: {json_mod.dumps({'step': 'kline', 'status': 'start', 'total': len(symbols_to_sync)})}\n\n"

        start_date = (datetime.now() - timedelta(days=1825)).strftime("%Y-%m-%d")
        end_date = datetime.now().strftime("%Y-%m-%d")
        success = 0
        failed = 0
        records = 0
        processed = 0
        src_used = {}

        def fetch_one(symbol):
            """经数据源路由获取单只股票标准K线（自动降级 + 单位统一）"""
            try:
                res = data_service.get_kline_data(symbol, "daily", start_date, end_date)
                if not res:
                    return symbol, None, None, "无数据"
                return symbol, res, res[0].get("source") if res else None, None
            except Exception as e:
                return symbol, None, None, str(e)[:100]

        # 使用线程池并行处理（最多5个并发）
        with ThreadPoolExecutor(max_workers=5) as executor:
            futures = {executor.submit(fetch_one, sym): sym for sym in symbols_to_sync}

            for future in as_completed(futures):
                symbol, data, src, error = future.result()
                processed += 1

                if error or not data:
                    failed += 1
                else:
                    # 写入数据库（标准口径：volume=股, amount=元；按终态规则覆盖）
                    try:
                        from services.market_writer import PRICE_FIELDS, upsert_daily_bars
                        rows_out = [{"trade_date": row.get("trade_date") or row.get("date"),
                                     **{f: row.get(f) for f in PRICE_FIELDS}} for row in data]
                        st = upsert_daily_bars(db_local, symbol, rows_out)
                        db_local.commit()
                        records += st["added"] + st["updated"]
                        success += 1
                        if src:
                            src_used[src] = src_used.get(src, 0) + 1
                    except Exception as e:
                        db_local.rollback()
                        failed += 1

                # 每处理10只推送一次进度
                if processed % 10 == 0 or processed == len(symbols_to_sync):
                    yield f"data: {json_mod.dumps({'step': 'kline', 'status': 'progress', 'current': processed, 'total': len(symbols_to_sync), 'success': success, 'failed': failed, 'records': records})}\n\n"

        db_local.close()
        yield f"data: {json_mod.dumps({'step': 'kline', 'status': 'done', 'success': success, 'failed': failed, 'records': records, 'sources': src_used})}\n\n"

        # Step 5: 同步到 Data Bridge（供 vibe-trading 消费）
        yield f"data: {json_mod.dumps({'step': 'bridge', 'status': 'start'})}\n\n"
        try:
            from datasources import bridge as _bridge
            exp = _bridge.export_watchlist_and_positions()
            env = _bridge.sync_source_order()
            yield f"data: {json_mod.dumps({'step': 'bridge', 'status': 'done', 'exported': exp['exported'], 'source_order': env['order']})}\n\n"
        except Exception as e:
            yield f"data: {json_mod.dumps({'step': 'bridge', 'status': 'error', 'message': str(e)[:120]})}\n\n"

        # Step 6: 同步一份到 Sequoia-X
        yield f"data: {json_mod.dumps({'step': 'sequoia', 'status': 'start'})}\n\n"
        try:
            from services import sequoia_sync
            res = sequoia_sync.sync_recent(days=30)
            yield f"data: {json_mod.dumps({'step': 'sequoia', 'status': 'done', 'synced': res.get('synced', 0)})}\n\n"
        except Exception as e:
            yield f"data: {json_mod.dumps({'step': 'sequoia', 'status': 'error', 'message': str(e)[:120]})}\n\n"

        yield f"data: {json_mod.dumps({'step': 'all_done', 'stock_count': len(stocks), 'info_added': info_added, 'kline_success': success, 'kline_records': records})}\n\n"

    return StreamingResponse(generate(), media_type="text/event-stream")


@router.post("/update-latest-stream")
def update_latest_stream(db: Session = Depends(get_db)):
    """更新最新数据（流式进度版）。

    与「全量更新」的区别：不是统一按某个日期批量写入，而是**以每只股票各自
    已有的最后交易日为起点，分别补齐到当前交易日**。适用于部分标的落后
    多天（长停牌复牌、曾漏抓）的场景。

    策略（两段，兼顾速度与完整性）：
      Step A 腾讯批量快照：确定当前交易日 T，为「只缺 T 这一天」的股票
             一次性批量补齐（300 只/批，快）。
      Step B 新浪日K补缺口：对仍落后（缺 T 之前多天）的股票，逐只按
             新浪日K补齐 [自末日期+1, T] 之间的所有缺失交易日。
    """
    from fastapi.responses import StreamingResponse
    import json as json_mod
    import time
    import urllib.error

    def generate():
        from models.market_data import MarketData, StockInfo
        from datasources import standards as std
        from database import SessionLocal

        def to_tencent(sym):
            code, _, ex = sym.partition(".")
            return ("sh" if ex.upper() == "SH" else "sz") + code

        # ---------- Step 0: 建立股票池 + 各自最后日期 ----------
        yield f"data: {json_mod.dumps({'step': 'init', 'status': 'start'})}\n\n"
        rows = (
            SessionLocal()
            .query(
                StockInfo.symbol,
                func.max(MarketData.trade_date),
            )
            .outerjoin(MarketData, StockInfo.symbol == MarketData.symbol)
            .group_by(StockInfo.symbol)
            .all()
        )
        symbols = [r[0] for r in rows]
        last_date = {r[0]: (str(r[1]) if r[1] else None) for r in rows}
        # 只保留沪深两市（北交所不分析）
        symbols = [s for s in symbols if std.is_tradable_a_stock(s)]
        yield f"data: {json_mod.dumps({'step': 'init', 'status': 'done', 'total': len(symbols), 'with_data': sum(1 for s in symbols if last_date.get(s))})}\n\n"

        # ---------- Step A: 腾讯批量快照，补齐「只缺当前交易日」的股票 ----------
        yield f"data: {json_mod.dumps({'step': 'snapshot', 'status': 'start'})}\n\n"
        UA = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
              "Referer": "https://qt.gtimg.cn/"}
        batch = 300
        snap = {}          # symbol -> 标准口径行情（含腾讯返回的日期）
        cur_date = None    # 当前交易日（取多数股票返回的日期）
        from collections import Counter
        date_votes = Counter()
        for i in range(0, len(symbols), batch):
            chunk = symbols[i:i + batch]
            codes = ",".join(to_tencent(s) for s in chunk)
            try:
                req = urllib.request.Request(f"https://qt.gtimg.cn/q={codes}", headers=UA)
                raw = urllib.request.urlopen(req, timeout=20).read().decode("gbk", "ignore")
            except Exception:
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
                fdate = f[30][:8] if len(f) > 30 and f[30] else None
                if fdate and len(fdate) == 8:
                    date_votes[f"{fdate[:4]}-{fdate[4:6]}-{fdate[6:8]}"] += 1
                snap[sym] = {
                    "trade_date": (f"{fdate[:4]}-{fdate[4:6]}-{fdate[6:8]}" if fdate and len(fdate) == 8 else None),
                    "close": close, "prev_close": num(4), "open": num(5),
                    "high": num(33), "low": num(34),
                    "volume": (num(6) or 0) * 100.0,
                    "amount": (num(37) or 0) * 1e4,
                    "turnover": num(38), "pct_change": num(32),
                }
            if (i // batch) % 6 == 0:
                yield f"data: {json_mod.dumps({'step': 'snapshot', 'status': 'progress', 'current': min(i + batch, len(symbols)), 'total': len(symbols)})}\n\n"
            time.sleep(0.12)

        cur_date = date_votes.most_common(1)[0][0] if date_votes else datetime.now().strftime("%Y-%m-%d")
        yield f"data: {json_mod.dumps({'step': 'snapshot', 'status': 'done', 'got': len(snap), 'cur_date': cur_date})}\n\n"

        # ---------- 落库：当日快照（终态判定 + 覆盖更新） ----------
        db2 = SessionLocal()
        added = updated = 0
        gap_symbols = []   # 仍落后（需要逐只补历史缺口）
        snap_is_final = None
        try:
            from services.market_clock import final_flag
            from services.market_writer import upsert_daily_bars

            # 交易日历（全市场已有日期），用于精确判断"某股在 cur_date 之前缺了哪些交易日"
            all_days = sorted(
                str(d) for (d,) in db2.query(MarketData.trade_date).distinct().all() if d
            )
            trade_days = [d for d in all_days if d < cur_date]
            try:
                cur_date_obj = datetime.strptime(cur_date, "%Y-%m-%d").date()
            except Exception:
                cur_date_obj = cur_date

            # 当日已有行的终态：1=已定格收盘价（不再覆盖），0/NULL=盘中快照（继续覆盖）
            today_state = {
                s: int(f or 0)
                for s, f in db2.query(MarketData.symbol, MarketData.is_final)
                .filter(MarketData.trade_date == cur_date_obj).all()
            }
            snap_is_final = final_flag(cur_date_obj)

            for sym in symbols:
                rec = snap.get(sym)
                last = last_date.get(sym)
                if not rec:
                    continue
                if last and last > cur_date:
                    continue                      # 库里有比快照更晚的数据，别倒退
                if today_state.get(sym) == 1:
                    continue                      # 已是收盘价，幂等跳过
                # 该股在 (last, cur_date) 之间是否还有其它缺失交易日？有则交给 Step B 补齐
                if last:
                    missing_hist = [d for d in trade_days if d > last]
                    if missing_hist:
                        gap_symbols.append(sym)

                row = dict(rec)
                row["trade_date"] = cur_date_obj
                st = upsert_daily_bars(db2, sym, [row])
                added += st["added"]
                updated += st["updated"]
            db2.commit()
        except Exception as e:
            db2.rollback()
            yield f"data: {json_mod.dumps({'step': 'snapshot', 'status': 'error', 'message': str(e)[:150]})}\n\n"
        finally:
            db2.close()

        yield f"data: {json_mod.dumps({'step': 'snapshot', 'status': 'saved', 'added': added, 'updated': updated, 'is_final': snap_is_final})}\n\n"

        # ---------- Step B: 新浪日K补齐多天缺口 ----------
        yield f"data: {json_mod.dumps({'step': 'gap', 'status': 'start', 'total': len(gap_symbols)})}\n\n"
        import ssl as _ssl
        import re as _re
        ctx = _ssl.create_default_context()
        ctx.check_hostname = False
        ctx.verify_mode = _ssl.CERT_NONE
        SH = {"User-Agent": "Mozilla/5.0", "Referer": "https://finance.sina.com.cn/"}
        gap_added = gap_ok = gap_fail = 0
        for idx, sym in enumerate(gap_symbols):
            code, _, ex = sym.partition(".")
            scode = ("sh" if ex.upper() == "SH" else "sz") + code
            start = last_date.get(sym)
            gap_err = ""
            try:
                url = (f"https://quotes.sina.cn/cn/api/jsonp_v2.php/var%20_{scode}="
                       f"/CN_MarketDataService.getKLineData?symbol={scode}&scale=240&ma=no&datalen=1400")
                txt = urllib.request.urlopen(urllib.request.Request(url, headers=SH), timeout=20,
                                             context=ctx).read().decode("utf-8", "ignore")
                m = _re.search(r"=\((\[.*?\])\)", txt, _re.S) or _re.search(r"=(\[.*?\])", txt, _re.S)
                klines = json_mod.loads(m.group(1)) if m else []
            except Exception as e:
                gap_err = str(e)[:100]
                klines = []
            if not klines:
                gap_fail += 1
                if gap_fail <= 3:
                    yield f"data: {json_mod.dumps({'step': 'gap', 'status': 'warn', 'symbol': sym, 'message': gap_err or '无K线数据'})}\n\n"
            else:
                db3 = SessionLocal()
                try:
                    have = {
                        str(d) for (d,) in db3.query(MarketData.trade_date).filter(
                            MarketData.symbol == sym
                        ).all()
                    }
                    prev_close = None
                    for k in klines:
                        d = k.get("day")
                        if not d or d in have:
                            prev_close = float(k["close"]) if k.get("close") else prev_close
                            continue
                        if start and d <= start:
                            prev_close = float(k["close"]) if k.get("close") else prev_close
                            continue
                        if d > cur_date:
                            continue
                        close = float(k["close"]) if k.get("close") else None
                        if not close:
                            continue
                        pct = round((close / prev_close - 1) * 100, 4) if prev_close else None
                        try:
                            d_obj = datetime.strptime(d, "%Y-%m-%d").date()
                        except Exception:
                            d_obj = d
                        db3.add(MarketData(
                            symbol=sym, trade_date=d_obj,
                            open=float(k["open"]) if k.get("open") else None,
                            high=float(k["high"]) if k.get("high") else None,
                            low=float(k["low"]) if k.get("low") else None,
                            close=close,
                            volume=float(k["volume"]) if k.get("volume") else None,
                            amount=None, turnover=None, pct_change=pct,
                            # 若补的正好是今天，需按当前时间判定是否已定格（盘中=0，收盘后=1）
                            is_final=final_flag(d_obj),
                        ))
                        gap_added += 1
                        prev_close = close
                    db3.commit()
                    gap_ok += 1
                except Exception:
                    db3.rollback()
                    gap_fail += 1
                finally:
                    db3.close()
            if (idx + 1) % 5 == 0 or idx + 1 == len(gap_symbols):
                yield f"data: {json_mod.dumps({'step': 'gap', 'status': 'progress', 'current': idx + 1, 'total': len(gap_symbols), 'ok': gap_ok, 'fail': gap_fail, 'records': gap_added})}\n\n"
            time.sleep(0.15)

        yield f"data: {json_mod.dumps({'step': 'gap', 'status': 'done', 'ok': gap_ok, 'fail': gap_fail, 'records': gap_added})}\n\n"

        # 同步一份到 Sequoia-X
        try:
            from services import sequoia_sync
            res = sequoia_sync.sync_recent(days=30)
            yield f"data: {json_mod.dumps({'step': 'sequoia', 'status': 'done', 'synced': res.get('synced', 0)})}\n\n"
        except Exception as e:
            yield f"data: {json_mod.dumps({'step': 'sequoia', 'status': 'error', 'message': str(e)[:120]})}\n\n"

        yield f"data: {json_mod.dumps({'step': 'all_done', 'cur_date': cur_date, 'snapshot_added': added, 'gap_added': gap_added, 'gap_symbols': len(gap_symbols)})}\n\n"

    return StreamingResponse(generate(), media_type="text/event-stream")


def _human_size(size_bytes: int) -> str:
    """字节数转可读大小"""
    if size_bytes < 1024:
        return f"{size_bytes} B"
    elif size_bytes < 1024 * 1024:
        return f"{size_bytes / 1024:.1f} KB"
    elif size_bytes < 1024 * 1024 * 1024:
        return f"{size_bytes / (1024 * 1024):.1f} MB"
    else:
        return f"{size_bytes / (1024 * 1024 * 1024):.2f} GB"


@router.get("/standards")
def get_data_standards():
    """数据标准：字段口径 + 数据源路由配置（供前端展示/排查）"""
    from datasources import standards as std
    from datasources import registry

    fields = {}
    for kind, specs in std.FIELD_REGISTRY.items():
        fields[kind] = [
            {"name": f.name, "unit": f.unit, "dtype": f.dtype,
             "precision": f.precision, "required": f.required, "description": f.description}
            for f in specs
        ]
    return {
        "fields": fields,
        "source_units": std.SOURCE_UNITS,
        "routes": registry.ROUTES,
        "sources": [
            {"name": n, "capabilities": sorted(s.capabilities),
             "volume_unit": s.raw_units.get("volume"), "amount_unit": s.raw_units.get("amount")}
            for n, s in registry.SOURCES.items()
        ],
    }


@router.get("/source-health")
def get_source_health():
    """数据源健康探测（逐个 ping）"""
    from datasources import registry
    out = []
    for name, src in registry.SOURCES.items():
        try:
            ok = src.healthcheck()
        except Exception:
            ok = False
        out.append({"source": name, "healthy": ok})
    return {"sources": out}


# ========== Data Bridge（quant <-> vibe 数据统一）==========
@router.get("/bar-status")
def get_bar_status(db: Session = Depends(get_db)):
    """当日行情是否已定格（收盘价）+ 仍处于盘中快照状态的交易日"""
    from services.market_clock import describe
    from services.market_writer import provisional_dates
    return {"clock": describe(), "provisional": provisional_dates(db)}


@router.get("/bridge/status")
def bridge_status():
    """查看 Data Bridge 状态：DuckDB 产物、vibe 配置、数据源顺序同步情况"""
    from datasources import bridge
    return bridge.status()


@router.post("/bridge/sync")
def bridge_sync(days: int = 1825):
    """触发桥接：导出持仓+自选的K线到 DuckDB，生成 vibe 配置，并同步数据源顺序。

    这是"数据统一"的一键入口——quant 取好数据后，vibe 即可零改代码消费。
    """
    from datasources import bridge
    export = bridge.export_watchlist_and_positions()
    env = bridge.sync_source_order()
    return {
        "status": "success",
        "export": export,
        "source_order": env,
        "message": f"已导出 {export['exported']} 只标的到 Data Bridge，"
                   f"并同步数据源顺序：{' → '.join(env['order'])}",
    }


@router.post("/bridge/export")
def bridge_export(symbols: str, days: int = 1825):
    """按指定标的（逗号分隔）导出到 Data Bridge"""
    from datasources import bridge
    syms = [s.strip() for s in symbols.split(",") if s.strip()]
    return bridge.export_symbols(syms, days)
