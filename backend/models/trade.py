"""交易记录模型"""
from sqlalchemy import Column, Integer, String, Float, DateTime, Date
from sqlalchemy.sql import func
from database import Base


class Trade(Base):
    """交易记录表"""
    __tablename__ = "trades"

    id = Column(Integer, primary_key=True, index=True)
    account_id = Column(Integer, nullable=False, index=True)
    symbol = Column(String(20), nullable=False, comment="股票代码")
    name = Column(String(100), comment="股票名称")
    market = Column(String(20), default="A股", comment="市场")
    direction = Column(String(10), nullable=False, comment="方向: buy/sell")
    price = Column(Float, nullable=False, comment="成交价格")
    quantity = Column(Integer, nullable=False, comment="成交数量")
    amount = Column(Float, nullable=False, comment="成交金额")
    commission = Column(Float, default=0.0, comment="佣金")
    stamp_tax = Column(Float, default=0.0, comment="印花税")
    transfer_fee = Column(Float, default=0.0, comment="过户费")
    total_fee = Column(Float, default=0.0, comment="总费用")
    strategy_id = Column(Integer, comment="关联策略ID")
    trade_date = Column(Date, nullable=False, comment="交易日期")
    trade_time = Column(String(20), comment="交易时间")
    created_at = Column(DateTime, server_default=func.now())
