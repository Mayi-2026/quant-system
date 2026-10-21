"""自选股票模型"""
from sqlalchemy import Column, Integer, String, Float, DateTime, Text
from sqlalchemy.sql import func
from database import Base


class Watchlist(Base):
    __tablename__ = "watchlist"

    id = Column(Integer, primary_key=True, autoincrement=True)
    symbol = Column(String(20), nullable=False, index=True)
    name = Column(String(50), default="")
    added_at = Column(DateTime, server_default=func.now())
    notes = Column(Text, default="")
    target_price = Column(Float, nullable=True)  # 压力位
    stop_loss = Column(Float, nullable=True)     # 支撑位
    rating = Column(String(20), nullable=True)   # 评级（TradingAgents）
    report = Column(Text, nullable=True)         # 报告 HTML 绝对路径
    rating_date = Column(String(20), nullable=True)  # 评级日期 YYYY-MM-DD
