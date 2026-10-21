"""市场数据 API"""
from fastapi import APIRouter, Query, HTTPException
from typing import List, Optional
from schemas import QuoteResponse, KlineData, MarketIndexResponse
from services.data_service import data_service

router = APIRouter(prefix="/api/market", tags=["市场数据"])


@router.get("/quote/{symbol}", response_model=QuoteResponse)
def get_quote(symbol: str):
    """获取股票实时行情"""
    quote = data_service.get_realtime_quote(symbol)
    if not quote:
        # 找不到该标的时返回 404，避免返回 {"error": ...} 触发 ResponseValidationError（500）
        raise HTTPException(status_code=404, detail=f"未找到该股票或行情不可用: {symbol}")
    return quote


@router.get("/kline/{symbol}", response_model=List[KlineData])
def get_kline(
    symbol: str,
    period: str = Query("daily", description="daily/weekly/monthly"),
    start_date: Optional[str] = None,
    end_date: Optional[str] = None,
    adjust: str = Query("qfq", description="qfq/hfq/空"),
):
    """获取K线数据"""
    return data_service.get_kline_data(
        symbol=symbol,
        period=period,
        start_date=start_date,
        end_date=end_date,
        adjust=adjust,
    )


@router.get("/indices", response_model=List[MarketIndexResponse])
def get_indices():
    """获取主要指数"""
    return data_service.get_market_indices()


@router.get("/search")
def search_stock(keyword: str = Query(..., description="搜索关键词")):
    """搜索股票"""
    return data_service.search_stock(keyword)


@router.get("/sectors")
def get_sectors():
    """获取行业板块资金流向"""
    return data_service.get_industry_sectors()


@router.get("/sector-overview")
def get_sector_overview():
    """A股行业板块全景：涨跌前5 + 资金流入流出前5 + 每板块前3股票"""
    return data_service.get_sector_overview()


@router.get("/us-overview")
def get_us_overview():
    """美股概览：指数 + 涨跌幅前5 + 跌幅前5"""
    return data_service.get_us_sector_overview()


@router.get("/scan")
def scan_stocks(condition: str = Query(..., description="选股条件中文描述")):
    """根据中文描述全市场扫描选股"""
    stocks = data_service.scan_stocks_by_condition(condition)
    return {"stocks": stocks, "count": len(stocks), "condition": condition}


@router.get("/breadth")
def get_market_breadth():
    """A股涨跌家数统计 + 成交量对比"""
    return data_service.get_market_breadth()


@router.get("/technical/{symbol}")
def get_technical(symbol: str, period: str = Query("daily", description="daily/weekly/monthly")):
    """获取技术指标：MA/MACD/KDJ/RSI/BOLL + 综合信号"""
    return data_service.get_technical_indicators(symbol, period)
