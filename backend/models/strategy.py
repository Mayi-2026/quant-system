"""策略模型"""
from sqlalchemy import Column, Integer, String, Float, DateTime, Date, Text, JSON
from sqlalchemy.sql import func
from database import Base


class Strategy(Base):
    """策略表"""
    __tablename__ = "strategies"

    id = Column(Integer, primary_key=True, index=True)
    name = Column(String(100), nullable=False, comment="策略名称")
    strategy_type = Column(String(50), nullable=False, comment="策略类型: momentum/mean_reversion/multi_factor/grid/event_driven")
    description = Column(Text, comment="策略描述")
    parameters = Column(JSON, comment="策略参数")
    status = Column(String(20), default="inactive", comment="状态: inactive/active/paused")
    market = Column(String(20), default="A股", comment="适用市场")
    timeframe = Column(String(20), default="daily", comment="时间周期: intraday/daily/weekly")
    total_return = Column(Float, default=0.0, comment="总收益率（小数形式，0.15 表示 15%）")
    win_rate = Column(Float, default=0.0, comment="胜率（小数形式）")
    max_drawdown = Column(Float, default=0.0, comment="最大回撤（小数形式，负值）")
    sharpe_ratio = Column(Float, default=0.0, comment="夏普比率")
    # Vibe-Trading 集成字段
    ai_generated = Column(Integer, default=0, comment="0=手动 1=AI 生成")
    vibe_run_id = Column(String(100), comment="最新回测 run_id")
    strategy_code = Column(Text, comment="AI 生成的策略代码")
    created_at = Column(DateTime, server_default=func.now())
    updated_at = Column(DateTime, server_default=func.now(), onupdate=func.now())


class StrategySignal(Base):
    """策略信号表"""
    __tablename__ = "strategy_signals"

    id = Column(Integer, primary_key=True, index=True)
    strategy_id = Column(Integer, nullable=False, index=True)
    symbol = Column(String(20), nullable=False, comment="股票代码")
    signal_type = Column(String(20), nullable=False, comment="信号类型: buy/sell/hold")
    signal_strength = Column(Float, default=1.0, comment="信号强度 0-1")
    price = Column(Float, comment="触发价格")
    reason = Column(Text, comment="信号原因")
    signal_date = Column(Date, nullable=False, comment="信号日期")
    executed = Column(Integer, default=0, comment="是否已执行: 0/1")
    created_at = Column(DateTime, server_default=func.now())
