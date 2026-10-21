"""市场数据模型"""
from sqlalchemy import Column, Integer, String, Float, DateTime, Date, Index
from sqlalchemy.sql import func
from database import Base


class StockInfo(Base):
    """股票基本信息表"""
    __tablename__ = "stock_info"

    id = Column(Integer, primary_key=True, index=True)
    symbol = Column(String(20), nullable=False, unique=True, comment="股票代码")
    name = Column(String(100), comment="股票名称")
    market = Column(String(20), comment="市场")
    industry = Column(String(50), comment="行业")
    list_date = Column(Date, comment="上市日期")
    is_st = Column(Integer, default=0, comment="是否ST")
    updated_at = Column(DateTime, server_default=func.now(), onupdate=func.now())

    __table_args__ = (
        Index("idx_symbol", "symbol"),
    )


class MarketData(Base):
    """行情数据表"""
    __tablename__ = "market_data"

    id = Column(Integer, primary_key=True, index=True)
    symbol = Column(String(20), nullable=False, comment="股票代码")
    trade_date = Column(Date, nullable=False, comment="交易日期")
    open = Column(Float, comment="开盘价")
    high = Column(Float, comment="最高价")
    low = Column(Float, comment="最低价")
    close = Column(Float, comment="收盘价")
    volume = Column(Float, comment="成交量")
    amount = Column(Float, comment="成交额")
    turnover = Column(Float, comment="换手率")
    pct_change = Column(Float, comment="涨跌幅")
    is_final = Column(Integer, default=1, server_default="1",
                      comment="1=日终收盘价（终态，不再覆盖）；0=盘中快照（可被后续覆盖）")
    created_at = Column(DateTime, server_default=func.now())

    __table_args__ = (
        Index("idx_symbol_date", "symbol", "trade_date"),
        # 按"交易日"聚合（涨跌家数/终态统计/数据体检）非常频繁，
        # 只有 (symbol, trade_date) 联合索引时会退化成全索引扫描（实测 11s）
        Index("idx_trade_date", "trade_date"),
    )
