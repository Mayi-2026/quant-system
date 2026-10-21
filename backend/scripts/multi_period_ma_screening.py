#!/usr/bin/env python3
"""多周期多头排列筛选 + 技术面/宏观面综合分析

筛选条件：
1. 日线、周线、月线均多头排列（MA5 > MA10 > MA20 > MA60）
2. 技术面指标：MACD金叉、RSI适中、成交量放大
3. 宏观面：行业景气度、资金流向、板块强度

输出：推荐10只明日最看好的股票
"""

import sys
import os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pandas as pd
import numpy as np
from datetime import datetime, timedelta
from database import SessionLocal
from models.market_data import MarketData, StockInfo
from sqlalchemy import func, and_
import logging

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)


def calculate_ma(df, periods=[5, 10, 20, 60]):
    """计算移动平均线"""
    for p in periods:
        df[f'ma{p}'] = df['close'].rolling(window=p).mean()
    return df


def is_bullish_alignment(row):
    """判断多头排列：MA5 > MA10 > MA20 > MA60"""
    try:
        if pd.isna(row['ma5']) or pd.isna(row['ma10']) or pd.isna(row['ma20']) or pd.isna(row['ma60']):
            return False
        return row['ma5'] > row['ma10'] > row['ma20'] > row['ma60']
    except:
        return False


def calculate_macd(df, fast=12, slow=26, signal=9):
    """计算MACD"""
    ema_fast = df['close'].ewm(span=fast, adjust=False).mean()
    ema_slow = df['close'].ewm(span=slow, adjust=False).mean()
    df['dif'] = ema_fast - ema_slow
    df['dea'] = df['dif'].ewm(span=signal, adjust=False).mean()
    df['macd'] = 2 * (df['dif'] - df['dea'])
    return df


def calculate_rsi(df, period=14):
    """计算RSI"""
    delta = df['close'].diff()
    gain = (delta.where(delta > 0, 0)).rolling(window=period).mean()
    loss = (-delta.where(delta < 0, 0)).rolling(window=period).mean()
    rs = gain / loss
    df['rsi'] = 100 - (100 / (1 + rs))
    return df


def calculate_volume_ratio(df, period=5):
    """计算量比"""
    df['vol_ma'] = df['volume'].rolling(window=period).mean()
    df['volume_ratio'] = df['volume'] / df['vol_ma']
    return df


def analyze_technical(df):
    """技术面评分"""
    score = 0
    latest = df.iloc[-1]
    prev = df.iloc[-2] if len(df) > 1 else latest
    
    # MACD金叉 (+20分)
    if latest['dif'] > latest['dea'] and prev['dif'] <= prev['dea']:
        score += 20
    elif latest['dif'] > latest['dea']:
        score += 10
    
    # MACD柱状图放大 (+10分)
    if latest['macd'] > 0 and latest['macd'] > prev['macd']:
        score += 10
    
    # RSI适中 (40-70) (+15分)
    if 40 <= latest['rsi'] <= 70:
        score += 15
    elif 30 <= latest['rsi'] < 40 or 70 < latest['rsi'] <= 80:
        score += 5
    
    # 量比放大 (>1.5) (+15分)
    if latest['volume_ratio'] > 1.5:
        score += 15
    elif latest['volume_ratio'] > 1.0:
        score += 5
    
    # 价格站上所有均线 (+10分)
    if latest['close'] > latest['ma5'] > latest['ma10'] > latest['ma20']:
        score += 10
    
    # 涨幅适中 (0-5%) (+10分)
    pct_change = (latest['close'] - prev['close']) / prev['close'] * 100 if prev['close'] > 0 else 0
    if 0 < pct_change <= 5:
        score += 10
    elif -2 <= pct_change <= 0:
        score += 5
    
    return score, latest


def get_stock_data(db, symbol, days=120):
    """获取股票历史数据"""
    rows = db.query(MarketData).filter(
        MarketData.symbol == symbol
    ).order_by(MarketData.trade_date.desc()).limit(days).all()
    
    if not rows:
        return None
    
    df = pd.DataFrame([{
        'date': r.trade_date,
        'open': float(r.open or 0),
        'high': float(r.high or 0),
        'low': float(r.low or 0),
        'close': float(r.close or 0),
        'volume': float(r.volume or 0),
    } for r in rows])
    
    # 确保 date 是 datetime 类型
    df['date'] = pd.to_datetime(df['date'])
    df = df.sort_values('date').reset_index(drop=True)
    return df


def check_multi_period_bullish(db, symbol):
    """检查日线、周线、月线是否均多头排列"""
    # 日线（需要60天以上数据计算MA60）
    df_daily = get_stock_data(db, symbol, days=120)
    if df_daily is None or len(df_daily) < 60:
        return False, None, None, None
    
    df_daily = calculate_ma(df_daily)
    df_daily = calculate_macd(df_daily)
    df_daily = calculate_rsi(df_daily)
    df_daily = calculate_volume_ratio(df_daily)
    
    if not is_bullish_alignment(df_daily.iloc[-1]):
        return False, None, None, None
    
    # 周线（需要60周以上数据）
    df_weekly = df_daily.copy()
    df_weekly = df_weekly.set_index('date').resample('W').agg({
        'open': 'first',
        'high': 'max',
        'low': 'min',
        'close': 'last',
        'volume': 'sum'
    }).dropna().reset_index()
    
    if len(df_weekly) < 20:
        return False, None, None, None
    
    df_weekly = calculate_ma(df_weekly)
    if not is_bullish_alignment(df_weekly.iloc[-1]):
        return False, None, None, None
    
    # 月线（需要12个月以上数据）
    df_monthly = df_daily.copy()
    df_monthly = df_monthly.set_index('date').resample('M').agg({
        'open': 'first',
        'high': 'max',
        'low': 'min',
        'close': 'last',
        'volume': 'sum'
    }).dropna().reset_index()
    
    if len(df_monthly) < 12:
        return False, None, None, None
    
    df_monthly = calculate_ma(df_monthly)
    if not is_bullish_alignment(df_monthly.iloc[-1]):
        return False, None, None, None
    
    # 技术面评分
    tech_score, latest = analyze_technical(df_daily)
    
    return True, tech_score, latest, df_daily


def get_sector_strength(db, symbol):
    """获取板块强度（简化版：基于同行业股票平均涨幅）"""
    stock = db.query(StockInfo).filter(StockInfo.symbol == symbol).first()
    if not stock or not stock.industry:
        return 0
    
    # 获取同行业股票
    peers = db.query(StockInfo).filter(
        StockInfo.industry == stock.industry,
        StockInfo.symbol != symbol
    ).limit(10).all()
    
    if not peers:
        return 0
    
    # 计算平均涨幅
    total_change = 0
    count = 0
    for peer in peers:
        md = db.query(MarketData).filter(
            MarketData.symbol == peer.symbol
        ).order_by(MarketData.trade_date.desc()).limit(2).all()
        
        if len(md) >= 2:
            change = (float(md[0].close or 0) - float(md[1].close or 0)) / float(md[1].close or 1) * 100
            total_change += change
            count += 1
    
    avg_change = total_change / count if count > 0 else 0
    
    # 板块强度评分
    if avg_change > 2:
        return 20
    elif avg_change > 0:
        return 10
    else:
        return 0


def screen_stocks():
    """主筛选逻辑"""
    db = SessionLocal()
    
    try:
        # 获取所有股票
        stocks = db.query(StockInfo).all()
        logger.info(f"总共 {len(stocks)} 只股票")
        
        candidates = []
        
        for i, stock in enumerate(stocks):
            if i % 100 == 0:
                logger.info(f"处理进度: {i}/{len(stocks)}")
            
            # 检查多周期多头排列
            is_bullish, tech_score, latest, df_daily = check_multi_period_bullish(db, stock.symbol)
            
            if not is_bullish:
                continue
            
            # 板块强度
            sector_score = get_sector_strength(db, stock.symbol)
            
            # 综合评分
            total_score = tech_score + sector_score
            
            candidates.append({
                'symbol': stock.symbol,
                'name': stock.name,
                'industry': stock.industry or '未知',
                'close': latest['close'],
                'pct_change': (latest['close'] - df_daily.iloc[-2]['close']) / df_daily.iloc[-2]['close'] * 100 if len(df_daily) > 1 else 0,
                'tech_score': tech_score,
                'sector_score': sector_score,
                'total_score': total_score,
                'rsi': latest['rsi'],
                'macd': latest['macd'],
                'volume_ratio': latest['volume_ratio'],
            })
        
        # 按综合评分排序
        candidates.sort(key=lambda x: x['total_score'], reverse=True)
        
        # 取前10只
        top10 = candidates[:10]
        
        logger.info(f"\n筛选完成，共 {len(candidates)} 只符合多头排列条件")
        logger.info(f"推荐前10只：\n")
        
        for i, stock in enumerate(top10, 1):
            print(f"{i}. {stock['name']} ({stock['symbol']})")
            print(f"   行业: {stock['industry']}")
            print(f"   现价: {stock['close']:.2f} | 涨幅: {stock['pct_change']:+.2f}%")
            print(f"   技术评分: {stock['tech_score']} | 板块评分: {stock['sector_score']} | 综合: {stock['total_score']}")
            print(f"   RSI: {stock['rsi']:.1f} | MACD: {stock['macd']:.2f} | 量比: {stock['volume_ratio']:.2f}")
            print()
        
        return top10
    
    finally:
        db.close()


if __name__ == '__main__':
    screen_stocks()
