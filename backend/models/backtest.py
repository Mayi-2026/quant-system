"""回测记录模型"""
from sqlalchemy import Column, Integer, String, Float, DateTime, Date, Text, ForeignKey
from sqlalchemy.sql import func
from database import Base


class BacktestRecord(Base):
    """回测记录表（由 Vibe-Trading 引擎产出）"""
    __tablename__ = "backtest_records"

    id = Column(Integer, primary_key=True, index=True)
    strategy_id = Column(Integer, nullable=False, index=True, comment="关联策略 ID")
    vibe_run_id = Column(String(100), nullable=False, comment="Vibe-Trading run_id")

    # 回测配置
    symbol = Column(String(20), comment="标的代码，如 600570.SH")
    symbol_name = Column(String(50), comment="标的名称，如 恒生电子")
    start_date = Column(Date)
    end_date = Column(Date)
    initial_cash = Column(Float, default=1000000)
    engine_type = Column(String(20), default="daily")

    # 核心指标（小数形式，0.30 表示 30%）
    final_value = Column(Float)
    total_return = Column(Float)
    annual_return = Column(Float)
    max_drawdown = Column(Float)
    sharpe_ratio = Column(Float)
    calmar_ratio = Column(Float)
    sortino_ratio = Column(Float)
    win_rate = Column(Float)
    profit_loss_ratio = Column(Float)
    trade_count = Column(Integer)
    avg_holding_days = Column(Float)
    max_consecutive_loss = Column(Integer)

    # 基准对比
    benchmark_return = Column(Float)
    excess_return = Column(Float)
    beta = Column(Float)

    # 产物路径
    run_dir = Column(String(500), comment="Vibe-Trading run 目录绝对路径")
    equity_csv_path = Column(String(500))
    trades_csv_path = Column(String(500))
    run_card_path = Column(String(500))
    strategy_code_path = Column(String(500))

    # 回测完成后生成的多头股票池与报告
    stock_pool = Column(Text, comment="回测完成时生成的多头股票池（JSON 数组）")
    report_path = Column(String(500), comment="回测报告 HTML 路径")

    # 状态
    status = Column(String(20), default="running", comment="running/completed/failed")
    error_message = Column(Text)

    created_at = Column(DateTime, server_default=func.now())
    completed_at = Column(DateTime)
