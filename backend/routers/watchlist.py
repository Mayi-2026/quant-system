"""自选股票 API"""
import logging
import os
from datetime import datetime

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Query
from fastapi.responses import FileResponse
from sqlalchemy.orm import Session
from typing import List, Optional
from database import SessionLocal, get_db
from models.watchlist import Watchlist
from services.data_service import data_service
from services import task_manager
from services.tradingagents_client import tradingagents_client

router = APIRouter(prefix="/api/watchlist", tags=["自选股票"])

logger = logging.getLogger(__name__)


def _compute_support_resistance(symbol: str, db: Session, lookback: int = 20):
    """基于本地行情近 lookback 个交易日高低点计算压力位/支撑位。

    压力位 = 近期最高价 max(high)，支撑位 = 近期最低价 min(low)。
    返回 (pressure, support)，无数据时为 (None, None)。
    """
    from models.market_data import MarketData

    rows = (
        db.query(MarketData.high, MarketData.low)
        .filter(MarketData.symbol == symbol)
        .order_by(MarketData.trade_date.desc())
        .limit(lookback)
        .all()
    )
    highs = [r[0] for r in rows if r[0] is not None]
    lows = [r[1] for r in rows if r[1] is not None]
    if not highs or not lows:
        return None, None
    return round(max(highs), 2), round(min(lows), 2)


@router.get("/search")
def search_stocks(keyword: str = Query(..., description="搜索关键词（股票代码或名称）")):
    """搜索股票（支持代码和名称模糊搜索，支持 ETF）"""
    from models.market_data import StockInfo
    from database import SessionLocal
    from datasources import standards as std

    db = SessionLocal()
    try:
        # 模糊搜索：匹配代码或名称（排除北交所）
        stocks = db.query(StockInfo).filter(
            (StockInfo.symbol.contains(keyword)) |
            (StockInfo.name.contains(keyword))
        ).limit(40).all()

        results = []
        for stock in stocks:
            if not std.is_tradable_a_stock(stock.symbol):
                continue
            results.append({
                "symbol": stock.symbol,
                "name": stock.name or "",
                "industry": stock.industry or "",
            })
            if len(results) >= 20:
                break

        # 如果本地没搜到，且关键词是 6 位数字代码，尝试用腾讯 API 验证（支持 ETF）
        if not results and keyword.isdigit() and len(keyword) == 6 and std.is_tradable_a_stock(keyword):
            # 推断交易所
            if keyword.startswith("6") or keyword.startswith("5"):
                symbol = f"{keyword}.SH"
            else:
                symbol = f"{keyword}.SZ"

            # 用腾讯 API 获取行情
            quote = data_service.get_realtime_quote(symbol)
            if quote and quote.get("price", 0) > 0:
                results.append({
                    "symbol": symbol,
                    "name": quote.get("name", ""),
                    "industry": "ETF" if keyword.startswith("15") or keyword.startswith("51") else "",
                })

        return {"stocks": results, "count": len(results)}
    finally:
        db.close()


@router.get("/")
def get_watchlist(db: Session = Depends(get_db)):
    """获取自选股列表（批量获取行情，高性能）"""
    items = db.query(Watchlist).order_by(Watchlist.added_at.desc()).all()
    if not items:
        return []

    # 批量获取所有自选股的行情
    symbols = [item.symbol for item in items]
    quotes = data_service.get_batch_quotes(symbols)

    result = []
    backfilled = False
    for item in items:
        if item.target_price is None or item.stop_loss is None:
            pressure, support = _compute_support_resistance(item.symbol, db)
            if pressure is not None:
                item.target_price = pressure
            if support is not None:
                item.stop_loss = support
            backfilled = True
        quote = quotes.get(item.symbol, {})
        result.append({
            "id": item.id,
            "symbol": item.symbol,
            "name": item.name or quote.get("name", ""),
            "added_at": str(item.added_at) if item.added_at else "",
            "notes": item.notes or "",
            "target_price": item.target_price,
            "stop_loss": item.stop_loss,
            "rating": item.rating,
            "report": item.report,
            "rating_date": item.rating_date,
            "price": quote.get("price", 0),
            "change_pct": quote.get("change_pct", 0),
            "volume": quote.get("volume", 0),
            "amount": quote.get("amount", 0),
            "high": quote.get("high", 0),
            "low": quote.get("low", 0),
        })
    if backfilled:
        db.commit()
    return result


@router.post("/")
def add_to_watchlist(data: dict, db: Session = Depends(get_db)):
    """添加自选股"""
    from datasources import standards as std

    symbol = data.get("symbol", "")
    if not symbol:
        return {"error": "股票代码不能为空"}
    if not std.is_tradable_a_stock(symbol):
        return {"error": "北交所股票暂不纳入分析范围"}
    existing = db.query(Watchlist).filter(Watchlist.symbol == symbol).first()
    if existing:
        return {"error": "该股票已在自选列表中"}
    item = Watchlist(
        symbol=symbol,
        name=data.get("name", ""),
        notes=data.get("notes", ""),
        target_price=data.get("target_price"),
        stop_loss=data.get("stop_loss"),
    )
    db.add(item)
    db.commit()
    db.refresh(item)
    return {"id": item.id, "symbol": item.symbol, "message": "添加成功"}


@router.delete("/{item_id}")
def remove_from_watchlist(item_id: int, db: Session = Depends(get_db)):
    """删除自选股"""
    item = db.query(Watchlist).filter(Watchlist.id == item_id).first()
    if not item:
        return {"error": "未找到该记录"}
    db.delete(item)
    db.commit()
    return {"message": "删除成功"}


@router.put("/{item_id}")
def update_watchlist_item(item_id: int, data: dict, db: Session = Depends(get_db)):
    """更新自选股备注/目标价/止损价"""
    item = db.query(Watchlist).filter(Watchlist.id == item_id).first()
    if not item:
        return {"error": "未找到该记录"}
    if "notes" in data:
        item.notes = data["notes"]
    if "target_price" in data:
        item.target_price = data["target_price"]
    if "stop_loss" in data:
        item.stop_loss = data["stop_loss"]
    db.commit()
    return {"message": "更新成功"}


def _generate_reports(task_id: str):
    """后台任务：为每一只自选股调用 TradingAgents 生成报告并回写评级/报告/评级日期"""
    db = SessionLocal()
    try:
        items = db.query(Watchlist).order_by(Watchlist.id.asc()).all()
        total = len(items)
        done = 0
        errors = []
        for item in items:
            task_manager.update_task(task_id, current_symbol=item.symbol, done=done, total=total)
            try:
                result = tradingagents_client.generate_report(item.symbol)
                item.rating = result.get("rating")
                item.report = result.get("report_html")
                item.rating_date = datetime.now().strftime("%Y-%m-%d")
            except Exception as exc:  # noqa: BLE001 — 单只失败不阻断其余
                logger.exception("TradingAgents 报告生成失败 symbol=%s", item.symbol)
                errors.append({"symbol": item.symbol, "error": str(exc)})
            done += 1
            db.commit()
        task_manager.update_task(task_id, status="completed", done=done, total=total, errors=errors)
    except Exception as exc:  # noqa: BLE001
        logger.exception("批量生成报告失败")
        task_manager.update_task(task_id, status="failed", error=str(exc))
    finally:
        db.close()


@router.post("/generate-reports")
def generate_reports(background_tasks: BackgroundTasks):
    """批量生成所有自选股的 TradingAgents 报告（后台任务）

    返回 {task_id}，前端轮询 GET /api/ai/tasks/{task_id}
    """
    task_id = task_manager.create_task("watchlist_reports")
    background_tasks.add_task(_generate_reports, task_id=task_id)
    logger.info("自选股报告任务已创建 task=%s", task_id)
    return {"task_id": task_id}


@router.get("/{item_id}/report")
def get_watchlist_report(item_id: int, db: Session = Depends(get_db)):
    """返回自选股的 TradingAgents HTML 报告"""
    item = db.query(Watchlist).filter(Watchlist.id == item_id).first()
    if not item or not item.report:
        raise HTTPException(status_code=404, detail="报告不存在")
    if not os.path.exists(item.report):
        raise HTTPException(status_code=404, detail="报告文件不存在")
    return FileResponse(item.report, media_type="text/html")
