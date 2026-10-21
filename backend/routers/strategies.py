"""策略管理 API"""
import json
import logging
from datetime import date, timedelta

from fastapi import APIRouter, Depends, HTTPException, BackgroundTasks
from sqlalchemy.orm import Session
from typing import List

from database import get_db
from models.strategy import Strategy, StrategySignal
from models.backtest import BacktestRecord
from schemas import StrategyCreate, StrategyResponse, StrategySignalResponse, BacktestRequest
from services.strategy_service import strategy_service
from services import task_manager
from services.backtest_runner import execute_backtest

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/strategies", tags=["策略管理"])


@router.get("/templates")
def list_templates():
    """获取策略模板"""
    return strategy_service.get_templates()


@router.get("/", response_model=List[StrategyResponse])
def list_strategies(db: Session = Depends(get_db)):
    """获取策略列表"""
    return db.query(Strategy).all()


@router.post("/", response_model=StrategyResponse)
def create_strategy(strategy: StrategyCreate, db: Session = Depends(get_db)):
    """新建策略"""
    db_strategy = Strategy(
        name=strategy.name,
        strategy_type=strategy.strategy_type,
        description=strategy.description,
        parameters=strategy.parameters,
        market=strategy.market,
        timeframe=strategy.timeframe,
        status="inactive",
    )
    db.add(db_strategy)
    db.commit()
    db.refresh(db_strategy)
    return db_strategy


@router.get("/{strategy_id}", response_model=StrategyResponse)
def get_strategy(strategy_id: int, db: Session = Depends(get_db)):
    """获取策略详情"""
    strategy = db.query(Strategy).filter(Strategy.id == strategy_id).first()
    if not strategy:
        raise HTTPException(status_code=404, detail="策略不存在")
    return strategy


@router.put("/{strategy_id}/status")
def update_strategy_status(strategy_id: int, status: str, db: Session = Depends(get_db)):
    """更新策略状态"""
    strategy = db.query(Strategy).filter(Strategy.id == strategy_id).first()
    if not strategy:
        raise HTTPException(status_code=404, detail="策略不存在")
    strategy.status = status
    db.commit()
    return {"message": f"策略状态已更新为 {status}"}


@router.put("/{strategy_id}/name")
def update_strategy_name(strategy_id: int, data: dict, db: Session = Depends(get_db)):
    """更新策略名称"""
    strategy = db.query(Strategy).filter(Strategy.id == strategy_id).first()
    if not strategy:
        raise HTTPException(status_code=404, detail="策略不存在")
    new_name = data.get("name", "").strip()
    if not new_name:
        raise HTTPException(status_code=400, detail="名称不能为空")
    strategy.name = new_name
    db.commit()
    return {"message": "策略名称已更新"}


@router.delete("/{strategy_id}")
def delete_strategy(strategy_id: int, db: Session = Depends(get_db)):
    """删除策略及其关联的回测记录"""
    strategy = db.query(Strategy).filter(Strategy.id == strategy_id).first()
    if not strategy:
        raise HTTPException(status_code=404, detail="策略不存在")
    # 删除关联的回测记录
    db.query(BacktestRecord).filter(BacktestRecord.strategy_id == strategy_id).delete()
    db.delete(strategy)
    db.commit()
    return {"message": "策略已删除"}


@router.get("/{strategy_id}/signals", response_model=List[StrategySignalResponse])
def get_signals(strategy_id: int, db: Session = Depends(get_db)):
    """获取策略信号"""
    return (
        db.query(StrategySignal)
        .filter(StrategySignal.strategy_id == strategy_id)
        .order_by(StrategySignal.signal_date.desc())
        .all()
    )


@router.post("/{strategy_id}/backtest")
def run_backtest(
    strategy_id: int,
    params: BacktestRequest,
    background_tasks: BackgroundTasks,
    db: Session = Depends(get_db),
):
    """运行策略回测（调用 Vibe-Trading 引擎，后台任务）

    返回 {task_id, backtest_id}，前端轮询 GET /api/ai/tasks/{task_id}
    """
    strategy = db.query(Strategy).filter(Strategy.id == strategy_id).first()
    if not strategy:
        raise HTTPException(status_code=404, detail="策略不存在")

    end_date = params.end_date or date.today()
    start_date = params.start_date or (end_date - timedelta(days=365 * 3))

    # 构造自然语言回测指令
    param_desc = ""
    if strategy.parameters:
        flat = [f"{k}={v}" for k, v in strategy.parameters.items()
                if isinstance(v, (int, float, str))]
        if flat:
            param_desc = f"，参数：{'，'.join(flat)}"

    prompt = (
        f"使用「{strategy.name}」策略{param_desc}"
        f"回测 {params.symbol}，区间 {start_date.isoformat()} 至 {end_date.isoformat()}，"
        f"初始资金 {params.initial_cash:.0f}"
    )

    # 预建 running 状态的回测记录
    record = BacktestRecord(
        strategy_id=strategy_id,
        vibe_run_id="pending",
        symbol=params.symbol,
        start_date=start_date,
        end_date=end_date,
        initial_cash=params.initial_cash,
        status="running",
    )
    db.add(record)
    db.commit()
    db.refresh(record)

    task_id = task_manager.create_task(
        "strategy_backtest",
        strategy_id=strategy_id,
        backtest_id=record.id,
        prompt=prompt,
    )
    background_tasks.add_task(
        execute_backtest,
        task_id=task_id,
        prompt=prompt,
        strategy_id=strategy_id,
        backtest_id=record.id,
    )
    logger.info("回测任务已创建 task=%s strategy=%s backtest=%s", task_id, strategy_id, record.id)

    return {"task_id": task_id, "backtest_id": record.id, "prompt": prompt}


@router.get("/backtest/latest")
def get_latest_backtest(strategy_id: int, db: Session = Depends(get_db)):
    """策略最新一次回测记录 ID"""
    record = (
        db.query(BacktestRecord)
        .filter(BacktestRecord.strategy_id == strategy_id)
        .order_by(BacktestRecord.id.desc())
        .first()
    )
    if not record:
        raise HTTPException(status_code=404, detail="该策略暂无回测记录")
    return {"id": record.id, "status": record.status}


@router.get("/backtest/{backtest_id}")
def get_backtest_detail(backtest_id: int, db: Session = Depends(get_db)):
    """回测详情：指标 + 净值曲线 + 交易明细 + 策略代码 + 股票池 + 报告"""
    from services.vibe_trading_client import vibe_client

    record = db.query(BacktestRecord).filter(BacktestRecord.id == backtest_id).first()
    if not record:
        raise HTTPException(status_code=404, detail="回测记录不存在")
    strategy = db.query(Strategy).filter(Strategy.id == record.strategy_id).first()

    detail = {
        "id": record.id,
        "strategy_id": record.strategy_id,
        "strategy_name": strategy.name if strategy else None,
        "status": record.status,
        "error_message": record.error_message,
        "symbol": record.symbol,
        "start_date": record.start_date.isoformat() if record.start_date else None,
        "end_date": record.end_date.isoformat() if record.end_date else None,
        "initial_cash": record.initial_cash,
        "final_value": record.final_value,
        "total_return": record.total_return,
        "annual_return": record.annual_return,
        "max_drawdown": record.max_drawdown,
        "sharpe_ratio": record.sharpe_ratio,
        "calmar_ratio": record.calmar_ratio,
        "sortino_ratio": record.sortino_ratio,
        "win_rate": record.win_rate,
        "profit_loss_ratio": record.profit_loss_ratio,
        "trade_count": record.trade_count,
        "avg_holding_days": record.avg_holding_days,
        "max_consecutive_loss": record.max_consecutive_loss,
        "benchmark_return": record.benchmark_return,
        "excess_return": record.excess_return,
        "beta": record.beta,
        "vibe_run_id": record.vibe_run_id,
        "stock_pool": (json.loads(record.stock_pool) if record.stock_pool else []),
        "report_path": record.report_path,
        "created_at": record.created_at.isoformat() if record.created_at else None,
        "completed_at": record.completed_at.isoformat() if record.completed_at else None,
        "equity": [],
        "trades": [],
        "strategy_code": strategy.strategy_code if strategy else None,
        "run_card": None,
    }

    if record.status == "completed" and record.run_dir:
        try:
            art = vibe_client.load_run_artifacts(record.run_dir)
            detail["equity"] = art["equity"]
            detail["trades"] = art["trades"]
            detail["run_card"] = art["run_card"]
        except Exception:  # noqa: BLE001 — 产物缺失不阻断详情返回
            logger.exception("读取回测产物失败 backtest=%s", backtest_id)

    return detail


@router.get("/backtest/{backtest_id}/report")
def get_backtest_report(backtest_id: int, db: Session = Depends(get_db)):
    """返回回测完成后生成的 HTML 报告"""
    from fastapi.responses import FileResponse

    record = db.query(BacktestRecord).filter(BacktestRecord.id == backtest_id).first()
    if not record or not record.report_path:
        raise HTTPException(status_code=404, detail="回测报告不存在")
    return FileResponse(record.report_path, media_type="text/html")
