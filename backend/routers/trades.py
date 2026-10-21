"""交易记录 API"""
from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session
from typing import List
from datetime import date
from database import get_db
from models.trade import Trade
from models.position import Position
from schemas import TradeCreate, TradeResponse

router = APIRouter(prefix="/api/trades", tags=["交易记录"])


def calc_trade_fees(amount: float, direction: str) -> dict:
    """计算交易费用"""
    commission = max(amount * 0.0003, 5)  # 佣金 万三，最低5元
    stamp_tax = amount * 0.001 if direction == "sell" else 0  # 印花税 千一（卖出）
    transfer_fee = amount * 0.00002  # 过户费 十万分之二
    total_fee = commission + stamp_tax + transfer_fee
    return {
        "commission": round(commission, 2),
        "stamp_tax": round(stamp_tax, 2),
        "transfer_fee": round(transfer_fee, 2),
        "total_fee": round(total_fee, 2),
    }


@router.get("/", response_model=List[TradeResponse])
def list_trades(
    account_id: int = 1,
    start_date: str = None,
    end_date: str = None,
    db: Session = Depends(get_db),
):
    """获取交易记录"""
    query = db.query(Trade).filter(Trade.account_id == account_id)
    if start_date:
        query = query.filter(Trade.trade_date >= start_date)
    if end_date:
        query = query.filter(Trade.trade_date <= end_date)
    return query.order_by(Trade.trade_date.desc(), Trade.trade_time.desc()).all()


@router.post("/", response_model=TradeResponse)
def create_trade(trade: TradeCreate, db: Session = Depends(get_db)):
    """新建交易记录"""
    amount = trade.price * trade.quantity
    fees = calc_trade_fees(amount, trade.direction)

    db_trade = Trade(
        account_id=trade.account_id,
        symbol=trade.symbol,
        name=trade.name,
        market=trade.market,
        direction=trade.direction,
        price=trade.price,
        quantity=trade.quantity,
        amount=amount,
        commission=fees["commission"],
        stamp_tax=fees["stamp_tax"],
        transfer_fee=fees["transfer_fee"],
        total_fee=fees["total_fee"],
        strategy_id=trade.strategy_id,
        trade_date=trade.trade_date,
        trade_time=trade.trade_time,
    )
    db.add(db_trade)

    # 更新持仓
    position = (
        db.query(Position)
        .filter(Position.account_id == trade.account_id, Position.symbol == trade.symbol)
        .first()
    )

    if trade.direction == "buy":
        if position:
            total_cost = position.avg_cost * position.quantity + trade.price * trade.quantity
            position.quantity += trade.quantity
            position.avg_cost = total_cost / position.quantity if position.quantity > 0 else 0
        else:
            position = Position(
                account_id=trade.account_id,
                symbol=trade.symbol,
                name=trade.name,
                market=trade.market,
                quantity=trade.quantity,
                avg_cost=trade.price,
                current_price=trade.price,
                market_value=trade.price * trade.quantity,
                buy_date=trade.trade_date,
            )
            db.add(position)
    else:  # sell
        if position:
            position.quantity -= trade.quantity
            if position.quantity <= 0:
                db.delete(position)

    db.commit()
    db.refresh(db_trade)
    return db_trade


@router.delete("/{trade_id}")
def delete_trade(trade_id: int, db: Session = Depends(get_db)):
    """删除交易记录"""
    trade = db.query(Trade).filter(Trade.id == trade_id).first()
    if not trade:
        raise HTTPException(status_code=404, detail="交易记录不存在")
    db.delete(trade)
    db.commit()
    return {"message": "已删除"}


@router.get("/summary")
def get_trade_summary(account_id: int = 1, db: Session = Depends(get_db)):
    """获取交易统计"""
    trades = db.query(Trade).filter(Trade.account_id == account_id).all()
    total_trades = len(trades)
    buy_count = sum(1 for t in trades if t.direction == "buy")
    sell_count = sum(1 for t in trades if t.direction == "sell")
    total_amount = sum(t.amount for t in trades)
    total_fees = sum(t.total_fee for t in trades)

    return {
        "total_trades": total_trades,
        "buy_count": buy_count,
        "sell_count": sell_count,
        "total_amount": round(total_amount, 2),
        "total_fees": round(total_fees, 2),
    }
