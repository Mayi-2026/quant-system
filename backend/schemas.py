"""Pydantic schemas for API validation"""
from pydantic import BaseModel, Field
from typing import Optional, List, Dict, Any
from datetime import date, datetime


# ========== Account ==========
class AccountBase(BaseModel):
    name: str
    broker: Optional[str] = None
    account_type: str = "simulated"
    initial_capital: float = 100000.0
    risk_preference: str = "balanced"


class AccountCreate(AccountBase):
    pass


class AccountResponse(AccountBase):
    id: int
    current_capital: float
    created_at: datetime
    updated_at: datetime

    class Config:
        from_attributes = True


# ========== Position ==========
class PositionBase(BaseModel):
    symbol: str
    name: Optional[str] = None
    market: str = "A股"
    quantity: int = 0
    avg_cost: float = 0.0


class PositionCreate(PositionBase):
    account_id: int


class PositionUpdate(BaseModel):
    quantity: Optional[int] = None
    avg_cost: Optional[float] = None


class PositionResponse(PositionBase):
    id: int
    account_id: int
    current_price: float
    market_value: float
    profit_loss: float
    profit_loss_pct: float
    position_pct: float
    day_change_pct: Optional[float] = None  # 当日涨跌幅
    day_profit_loss: Optional[float] = None  # 当日盈亏
    buy_date: Optional[date] = None
    updated_at: datetime

    class Config:
        from_attributes = True


# ========== Trade ==========
class TradeBase(BaseModel):
    symbol: str
    name: Optional[str] = None
    market: str = "A股"
    direction: str  # buy/sell
    price: float
    quantity: int
    trade_date: date
    trade_time: Optional[str] = None
    strategy_id: Optional[int] = None


class TradeCreate(TradeBase):
    account_id: int


class TradeResponse(TradeBase):
    id: int
    account_id: int
    amount: float
    commission: float
    stamp_tax: float
    transfer_fee: float
    total_fee: float
    created_at: datetime

    class Config:
        from_attributes = True


# ========== Strategy ==========
class StrategyBase(BaseModel):
    name: str
    strategy_type: str
    description: Optional[str] = None
    parameters: Optional[Dict[str, Any]] = None
    market: str = "A股"
    timeframe: str = "daily"


class StrategyCreate(StrategyBase):
    pass


class StrategyResponse(StrategyBase):
    id: int
    status: str
    total_return: float
    win_rate: float
    max_drawdown: float
    sharpe_ratio: float
    ai_generated: int = 0
    vibe_run_id: Optional[str] = None
    created_at: datetime
    updated_at: datetime

    class Config:
        from_attributes = True


class BacktestRequest(BaseModel):
    """回测请求参数"""
    symbol: str
    start_date: Optional[date] = None
    end_date: Optional[date] = None
    initial_cash: float = 1000000


class StrategySignalResponse(BaseModel):
    id: int
    strategy_id: int
    symbol: str
    signal_type: str
    signal_strength: float
    price: Optional[float] = None
    reason: Optional[str] = None
    signal_date: date
    executed: int
    created_at: datetime

    class Config:
        from_attributes = True


# ========== Market Data ==========
class StockInfoResponse(BaseModel):
    symbol: str
    name: Optional[str] = None
    market: Optional[str] = None
    industry: Optional[str] = None
    list_date: Optional[date] = None
    is_st: int = 0

    class Config:
        from_attributes = True


class KlineData(BaseModel):
    date: str
    open: float
    high: float
    low: float
    close: float
    volume: float
    amount: Optional[float] = None
    pct_change: Optional[float] = None


class MarketIndexResponse(BaseModel):
    name: str
    value: float
    change_pct: float
    change: float


class DashboardResponse(BaseModel):
    """首页仪表盘数据"""
    total_assets: float
    today_pnl: float
    today_pnl_pct: float
    position_value: float
    available_cash: float
    total_return_pct: float
    positions: List[PositionResponse]
    indices: List[MarketIndexResponse]
    asset_curve: List[Dict[str, Any]]
    allocation: List[Dict[str, Any]]


class QuoteResponse(BaseModel):
    """股票实时行情"""
    symbol: str
    name: str
    price: float
    change: float
    change_pct: float
    open: float
    high: float
    low: float
    prev_close: float
    volume: float
    amount: float
    turnover: Optional[float] = None
    pe_ratio: Optional[float] = None
    pb_ratio: Optional[float] = None
    market_cap: Optional[float] = None
    float_cap: Optional[float] = None
    limit_up: Optional[float] = None
    limit_down: Optional[float] = None


class DataSyncResponse(BaseModel):
    """数据同步响应"""
    status: str
    message: str
    count: int
    symbols: List[str]
