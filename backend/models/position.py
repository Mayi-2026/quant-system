"""持仓与账户模型"""
from sqlalchemy import Column, Integer, String, Float, DateTime, Date
from sqlalchemy.sql import func
from database import Base


class Account(Base):
    """账户表"""
    __tablename__ = "accounts"

    id = Column(Integer, primary_key=True, index=True)
    name = Column(String(100), nullable=False, comment="账户名称")
    broker = Column(String(50), comment="券商")
    account_type = Column(String(20), default="simulated", comment="账户类型: simulated/live")
    initial_capital = Column(Float, default=100000.0, comment="初始资金")
    current_capital = Column(Float, default=100000.0, comment="当前资金")
    risk_preference = Column(String(20), default="balanced", comment="风险偏好: conservative/balanced/aggressive")
    created_at = Column(DateTime, server_default=func.now())
    updated_at = Column(DateTime, server_default=func.now(), onupdate=func.now())


class Position(Base):
    """持仓表"""
    __tablename__ = "positions"

    id = Column(Integer, primary_key=True, index=True)
    account_id = Column(Integer, nullable=False, index=True)
    symbol = Column(String(20), nullable=False, comment="股票代码，如 600519.SH")
    name = Column(String(100), comment="股票名称")
    market = Column(String(20), default="A股", comment="市场")
    quantity = Column(Integer, default=0, comment="持仓数量")
    avg_cost = Column(Float, default=0.0, comment="平均成本")
    current_price = Column(Float, default=0.0, comment="当前价格")
    market_value = Column(Float, default=0.0, comment="市值")
    profit_loss = Column(Float, default=0.0, comment="浮动盈亏")
    profit_loss_pct = Column(Float, default=0.0, comment="盈亏百分比")
    position_pct = Column(Float, default=0.0, comment="仓位占比")
    buy_date = Column(Date, comment="买入日期")
    created_at = Column(DateTime, server_default=func.now())
    updated_at = Column(DateTime, server_default=func.now(), onupdate=func.now())
