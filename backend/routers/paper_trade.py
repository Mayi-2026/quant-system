"""模拟交易路由"""
from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session
from typing import List, Optional
from pydantic import BaseModel

from database import get_db
from services.paper_trade_service import paper_trade_service

router = APIRouter(prefix="/api/paper", tags=["模拟交易"])


# ========== 请求体 ==========

class CreateAccountRequest(BaseModel):
    name: str = "模拟盘"
    initial_capital: float = 1000000.0


class PlaceOrderRequest(BaseModel):
    symbol: str
    direction: str  # buy / sell
    quantity: int
    order_type: str = "market"
    price: Optional[float] = None
    strategy_id: Optional[int] = None
    reason: Optional[str] = None


# ========== 账户 ==========

@router.post("/accounts")
def create_account(req: CreateAccountRequest, db: Session = Depends(get_db)):
    account = paper_trade_service.create_account(db, req.name, req.initial_capital)
    return {"id": account.id, "name": account.name, "initial_capital": account.initial_capital}


@router.get("/accounts")
def list_accounts(db: Session = Depends(get_db)):
    accounts = paper_trade_service.get_accounts(db)
    result = []
    for a in accounts:
        summary = paper_trade_service.get_account_summary(db, a.id)
        result.append(summary)
    return result


@router.get("/accounts/{account_id}")
def get_account(account_id: int, db: Session = Depends(get_db)):
    summary = paper_trade_service.get_account_summary(db, account_id)
    if not summary:
        raise HTTPException(404, "账户不存在")
    return summary


# ========== 持仓 ==========

@router.get("/accounts/{account_id}/positions")
def get_positions(account_id: int, db: Session = Depends(get_db)):
    paper_trade_service.update_position_prices(db, account_id)
    positions = paper_trade_service.get_positions(db, account_id)
    result = []
    for p in positions:
        mv = p.quantity * p.current_price
        pl = (p.current_price - p.avg_cost) * p.quantity
        pl_pct = (p.current_price - p.avg_cost) / p.avg_cost * 100 if p.avg_cost else 0
        result.append({
            "id": p.id,
            "symbol": p.symbol,
            "name": p.name,
            "quantity": p.quantity,
            "avg_cost": round(p.avg_cost, 3),
            "current_price": round(p.current_price, 3),
            "market_value": round(mv, 2),
            "profit_loss": round(pl, 2),
            "profit_loss_pct": round(pl_pct, 2),
        })
    return result


# ========== 订单 ==========

@router.post("/accounts/{account_id}/orders")
def place_order(account_id: int, req: PlaceOrderRequest, db: Session = Depends(get_db)):
    try:
        order = paper_trade_service.place_order(
            db, account_id, req.symbol, req.direction,
            req.quantity, req.order_type, req.price,
            req.strategy_id, req.reason,
        )
        return {
            "id": order.id,
            "status": order.status,
            "symbol": order.symbol,
            "name": order.name,
            "direction": order.direction,
            "quantity": order.quantity,
            "filled_price": order.filled_price,
            "filled_quantity": order.filled_quantity,
        }
    except ValueError as e:
        raise HTTPException(400, str(e))


@router.get("/accounts/{account_id}/orders")
def list_orders(account_id: int, limit: int = 50, db: Session = Depends(get_db)):
    orders = paper_trade_service.get_orders(db, account_id, limit)
    return [
        {
            "id": o.id,
            "symbol": o.symbol,
            "name": o.name,
            "direction": o.direction,
            "price": o.price,
            "quantity": o.quantity,
            "filled_price": o.filled_price,
            "filled_quantity": o.filled_quantity,
            "status": o.status,
            "signal_reason": o.signal_reason,
            "strategy_id": o.strategy_id,
            "created_at": o.created_at.isoformat() if o.created_at else None,
            "filled_at": o.filled_at.isoformat() if o.filled_at else None,
        }
        for o in orders
    ]


@router.post("/orders/{order_id}/cancel")
def cancel_order(order_id: int, db: Session = Depends(get_db)):
    order = paper_trade_service.cancel_order(db, order_id)
    if not order:
        raise HTTPException(404, "订单不存在")
    return {"id": order.id, "status": order.status}


# ========== 信号 ==========

@router.post("/accounts/{account_id}/signals")
def generate_signals(account_id: int, strategy_id: Optional[int] = None, db: Session = Depends(get_db)):
    signals = paper_trade_service.generate_signals(db, account_id, strategy_id)
    return signals


@router.post("/accounts/{account_id}/execute")
def execute_signals(account_id: int, strategy_id: Optional[int] = None, db: Session = Depends(get_db)):
    """生成信号并立即执行"""
    signals = paper_trade_service.generate_signals(db, account_id, strategy_id)
    if not signals:
        return {"signals": 0, "orders": 0, "message": "当前无交易信号"}
    orders = paper_trade_service.execute_signals(db, account_id, signals)
    return {
        "signals": len(signals),
        "orders": len(orders),
        "signal_details": signals,
        "order_results": [
            {"id": o.id, "symbol": o.symbol, "direction": o.direction, "status": o.status}
            for o in orders
        ],
    }


# ========== 日志 ==========

@router.get("/accounts/{account_id}/logs")
def get_logs(account_id: int, limit: int = 100, db: Session = Depends(get_db)):
    logs = paper_trade_service.get_logs(db, account_id, limit)
    return [
        {
            "id": l.id,
            "event_type": l.event_type,
            "level": l.level,
            "message": l.message,
            "created_at": l.created_at.isoformat() if l.created_at else None,
        }
        for l in logs
    ]
