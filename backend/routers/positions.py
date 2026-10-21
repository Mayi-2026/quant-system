"""持仓管理 API"""
from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session
from typing import List
from database import get_db
from models.position import Position, Account
from schemas import PositionCreate, PositionResponse, PositionUpdate, AccountCreate, AccountResponse
from services.data_service import data_service

router = APIRouter(prefix="/api/positions", tags=["持仓管理"])


@router.get("/", response_model=List[PositionResponse])
def list_positions(account_id: int = 1, db: Session = Depends(get_db)):
    """获取持仓列表（批量获取行情，高性能）"""
    positions = db.query(Position).filter(Position.account_id == account_id).all()

    if not positions:
        return []

    # 批量获取所有持仓股票的行情（一次请求）
    symbols = [pos.symbol for pos in positions]
    quotes = data_service.get_batch_quotes(symbols)

    # 更新实时价格（只在内存中更新，最后统一提交）
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
            # 如果名称为空或等于代码，从行情中获取
            if (not pos.name or pos.name == pos.symbol) and quote.get("name"):
                pos.name = quote["name"]
            # 当日涨跌幅和当日盈亏
            prev_close = quote.get("prev_close", 0)
            if prev_close > 0:
                pos.day_change_pct = (pos.current_price / prev_close - 1) * 100
                pos.day_profit_loss = (pos.current_price - prev_close) * pos.quantity
            else:
                pos.day_change_pct = 0
                pos.day_profit_loss = 0

    # 统一提交一次
    db.commit()

    # 计算仓位占比
    total_value = sum(p.market_value for p in positions)
    for pos in positions:
        pos.position_pct = (pos.market_value / total_value * 100) if total_value > 0 else 0

    return positions


@router.post("/", response_model=PositionResponse)
def create_position(position: PositionCreate, db: Session = Depends(get_db)):
    """新建持仓"""
    # 检查是否已存在相同持仓
    existing = db.query(Position).filter(
        Position.account_id == position.account_id,
        Position.symbol == position.symbol,
    ).first()
    if existing:
        raise HTTPException(status_code=400, detail="该股票已在持仓中，不能重复添加")

    # 获取实时价格
    quote = data_service.get_realtime_quote(position.symbol)
    current_price = quote["price"] if quote else 0

    db_position = Position(
        account_id=position.account_id,
        symbol=position.symbol,
        name=position.name or (quote["name"] if quote else ""),
        market=position.market,
        quantity=position.quantity,
        avg_cost=position.avg_cost,
        current_price=current_price,
        market_value=position.quantity * current_price,
        profit_loss=(current_price - position.avg_cost) * position.quantity,
        profit_loss_pct=(
            (current_price / position.avg_cost - 1) * 100
            if position.avg_cost > 0
            else 0
        ),
    )
    db.add(db_position)
    db.commit()
    db.refresh(db_position)
    return db_position


@router.put("/{position_id}", response_model=PositionResponse)
def update_position(position_id: int, update: PositionUpdate, db: Session = Depends(get_db)):
    """更新持仓"""
    position = db.query(Position).filter(Position.id == position_id).first()
    if not position:
        raise HTTPException(status_code=404, detail="持仓不存在")

    if update.quantity is not None:
        position.quantity = update.quantity
    if update.avg_cost is not None:
        position.avg_cost = update.avg_cost

    # 重新计算
    position.market_value = position.quantity * position.current_price
    position.profit_loss = (position.current_price - position.avg_cost) * position.quantity
    position.profit_loss_pct = (
        (position.current_price / position.avg_cost - 1) * 100
        if position.avg_cost > 0
        else 0
    )

    db.commit()
    db.refresh(position)
    return position


@router.delete("/{position_id}")
def delete_position(position_id: int, db: Session = Depends(get_db)):
    """删除持仓"""
    position = db.query(Position).filter(Position.id == position_id).first()
    if not position:
        raise HTTPException(status_code=404, detail="持仓不存在")
    db.delete(position)
    db.commit()
    return {"message": "已删除"}


# ========== 账户 ==========
@router.get("/accounts", response_model=List[AccountResponse])
def list_accounts(db: Session = Depends(get_db)):
    """获取账户列表"""
    return db.query(Account).all()


@router.post("/accounts", response_model=AccountResponse)
def create_account(account: AccountCreate, db: Session = Depends(get_db)):
    """新建账户"""
    db_account = Account(
        name=account.name,
        broker=account.broker,
        account_type=account.account_type,
        initial_capital=account.initial_capital,
        current_capital=account.initial_capital,
        risk_preference=account.risk_preference,
    )
    db.add(db_account)
    db.commit()
    db.refresh(db_account)
    return db_account
