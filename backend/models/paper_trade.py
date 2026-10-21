"""模拟交易模型"""
from sqlalchemy import Column, Integer, String, Float, DateTime, Date, Boolean, Text
from sqlalchemy.sql import func
from database import Base


class PaperAccount(Base):
    """模拟交易账户"""
    __tablename__ = "paper_accounts"

    id = Column(Integer, primary_key=True, index=True)
    name = Column(String(100), nullable=False, comment="账户名称")
    initial_capital = Column(Float, default=1000000.0, comment="初始资金")
    current_cash = Column(Float, default=1000000.0, comment="当前现金")
    status = Column(String(20), default="active", comment="状态: active/paused/closed")
    created_at = Column(DateTime, server_default=func.now())
    updated_at = Column(DateTime, server_default=func.now(), onupdate=func.now())


class PaperPosition(Base):
    """模拟持仓"""
    __tablename__ = "paper_positions"

    id = Column(Integer, primary_key=True, index=True)
    account_id = Column(Integer, nullable=False, index=True)
    symbol = Column(String(20), nullable=False, comment="股票代码")
    name = Column(String(100), comment="股票名称")
    quantity = Column(Integer, default=0, comment="持仓数量")
    avg_cost = Column(Float, default=0.0, comment="平均成本")
    current_price = Column(Float, default=0.0, comment="当前价格")
    created_at = Column(DateTime, server_default=func.now())
    updated_at = Column(DateTime, server_default=func.now(), onupdate=func.now())


class PaperOrder(Base):
    """模拟订单"""
    __tablename__ = "paper_orders"

    id = Column(Integer, primary_key=True, index=True)
    account_id = Column(Integer, nullable=False, index=True)
    strategy_id = Column(Integer, comment="关联策略ID")
    symbol = Column(String(20), nullable=False, comment="股票代码")
    name = Column(String(100), comment="股票名称")
    direction = Column(String(10), nullable=False, comment="方向: buy/sell")
    order_type = Column(String(20), default="market", comment="订单类型: market/limit")
    price = Column(Float, comment="委托价格")
    quantity = Column(Integer, nullable=False, comment="委托数量")
    filled_price = Column(Float, comment="成交价格")
    filled_quantity = Column(Integer, default=0, comment="成交数量")
    status = Column(String(20), default="pending", comment="状态: pending/filled/cancelled/rejected")
    signal_reason = Column(Text, comment="信号原因")
    created_at = Column(DateTime, server_default=func.now())
    filled_at = Column(DateTime, comment="成交时间")


class PaperTradeLog(Base):
    """模拟交易日志"""
    __tablename__ = "paper_trade_logs"

    id = Column(Integer, primary_key=True, index=True)
    account_id = Column(Integer, nullable=False, index=True)
    event_type = Column(String(30), nullable=False, comment="事件类型: signal/order/fill/error")
    level = Column(String(10), default="info", comment="级别: info/warn/error")
    message = Column(Text, nullable=False, comment="日志内容")
    data = Column(Text, comment="附加数据(JSON)")
    created_at = Column(DateTime, server_default=func.now())
