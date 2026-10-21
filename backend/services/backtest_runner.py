"""回测执行器：打通 vibe-trading CLI → 数据库记录 → 任务状态

供 AI 助手路由与策略回测路由共用，运行于 FastAPI BackgroundTasks 线程。
"""
import json
import logging
from datetime import datetime

from database import SessionLocal, retry_on_lock
from models.backtest import BacktestRecord
from models.strategy import Strategy
from services import task_manager
from services.vibe_trading_client import vibe_client, VibeTradingError
from services.stock_pool_service import screen_daily_bullish, generate_report_html

logger = logging.getLogger(__name__)


def _generate_strategy_name(symbol: str, prompt: str) -> str:
    """根据标的与策略描述生成策略名称"""
    desc = prompt.strip()[:20]
    symbol_part = f"（{symbol}）" if symbol else ""
    return f"{desc}{symbol_part}"


def _summarize(symbol: str, metrics: dict, start: str, end: str) -> str:
    """生成给前端的 AI 回复摘要"""

    def pct(v):
        return f"{v * 100:+.2f}%" if v is not None else "-"

    def pct0(v):
        return f"{v * 100:.2f}%" if v is not None else "-"

    sharpe = metrics.get("sharpe_ratio")
    parts = [
        f"已完成 {symbol or '标的'} 回测，区间 {start} 至 {end}。",
        "",
        f"核心结论：累计收益 {pct(metrics.get('total_return'))}，"
        f"年化 {pct(metrics.get('annual_return'))}，"
        f"最大回撤 {pct0(metrics.get('max_drawdown'))}，"
        f"夏普 {sharpe:.2f}，" if sharpe is not None else None,
        f"胜率 {pct0(metrics.get('win_rate'))}，"
        f"共 {metrics.get('trade_count') or 0} 笔交易。",
        "策略代码已生成并保存到策略库。",
    ]
    return "\n".join(p for p in parts if p is not None)


def execute_backtest(
    task_id: str,
    prompt: str,
    strategy_id: int = None,
    session_id: int = None,
    user_message: str = None,
    backtest_id: int = None,
):
    """执行一次完整回测并落库（阻塞，后台线程调用）

    - strategy_id 为空时：AI 新建策略
    - strategy_id 非空时：更新已有策略的指标与代码
    - backtest_id 非空时：回填已预建的 running 记录；否则新建
    - session_id 非空时：追加 AI assistant 消息
    """
    def _progress_callback(msg: str):
        """将进度信息写入 task，供前端轮询"""
        task_manager.update_task(task_id, progress=msg)

    db = SessionLocal()
    try:
        # 1. 调用 vibe-trading（耗时最长）
        _progress_callback("正在启动 Vibe-Trading CLI...")
        result = vibe_client.run_backtest(prompt, progress_callback=_progress_callback)
        fields = vibe_client.build_backtest_record_fields(result["run_dir"])
        metrics = fields["metrics"]

        # 2-3. 策略与回测记录落库
        # 整段逻辑封装为可重入函数：一旦遇到 "database is locked"，
        # 回滚并整体重跑（重新查询/构造对象），避免 rollback 后待提交对象丢失。
        def _persist():
            # 2. 策略落库
            if strategy_id:
                strategy = db.query(Strategy).filter(Strategy.id == strategy_id).first()
                if not strategy:
                    raise VibeTradingError(f"策略 {strategy_id} 不存在")
            else:
                strategy = Strategy(
                    name=_generate_strategy_name(fields.get("symbol"), user_message or prompt),
                    strategy_type="ai_generated",
                    description=(user_message or prompt)[:200],
                    parameters={},
                    market="A股",
                    timeframe="daily",
                    status="inactive",
                    ai_generated=1,
                )
                db.add(strategy)
                db.flush()

            strategy.strategy_code = fields["strategy_code"] or strategy.strategy_code
            strategy.vibe_run_id = fields["vibe_run_id"]
            strategy.total_return = metrics.get("total_return") or 0.0
            strategy.win_rate = metrics.get("win_rate") or 0.0
            strategy.max_drawdown = metrics.get("max_drawdown") or 0.0
            strategy.sharpe_ratio = metrics.get("sharpe_ratio") or 0.0
            db.flush()

            # 3. 回测记录落库（预建则回填）
            metric_kwargs = {k: v for k, v in metrics.items()}
            if backtest_id:
                record = db.query(BacktestRecord).filter(BacktestRecord.id == backtest_id).first()
                if not record:
                    raise VibeTradingError(f"回测记录 {backtest_id} 不存在")
                for k, v in metric_kwargs.items():
                    setattr(record, k, v)
            else:
                record = BacktestRecord(
                    strategy_id=strategy.id,
                    status="running",
                    **metric_kwargs,
                )
                db.add(record)

            record.strategy_id = strategy.id
            record.vibe_run_id = fields["vibe_run_id"]
            record.run_dir = fields["run_dir"]
            record.symbol = fields.get("symbol")
            record.start_date = fields.get("start_date")
            record.end_date = fields.get("end_date")
            record.initial_cash = fields.get("initial_cash")
            record.engine_type = fields.get("engine_type")
            record.equity_csv_path = fields["paths"]["equity_csv"]
            record.trades_csv_path = fields["paths"]["trades_csv"]
            record.run_card_path = fields["paths"]["run_card"]
            record.status = "completed"
            record.completed_at = datetime.now()
            db.commit()
            db.refresh(record)
            return strategy, record

        strategy, record = retry_on_lock(_persist, db)

        # 3.5 每次回测完成后生成多头股票池与独立 HTML 报告
        try:
            _progress_callback("正在生成多头股票池与回测报告...")
            pool, _total = screen_daily_bullish(db, top_n=10)
            report_path = generate_report_html(pool, {
                "backtest_id": record.id,
                "strategy_name": strategy.name,
                "symbol": fields.get("symbol"),
                "start_date": fields.get("start_date"),
                "end_date": fields.get("end_date"),
                "metrics": metrics,
            })

            def _persist_pool():
                rec = db.query(BacktestRecord).filter(BacktestRecord.id == record.id).first()
                if rec and not rec.stock_pool:
                    rec.stock_pool = json.dumps(pool, ensure_ascii=False)
                    rec.report_path = report_path
                    db.commit()

            retry_on_lock(_persist_pool, db)
        except Exception as e:  # noqa: BLE001 — 股票池/报告失败不阻断回测本身
            logger.exception("生成股票池/报告失败 backtest=%s：%s", record.id, e)

        # 4. AI 会话追加 assistant 消息
        summary = _summarize(fields.get("symbol"), metrics, fields.get("start_date"), fields.get("end_date"))
        if session_id:
            from models.ai_chat import AiChatMessage
            meta = {
                "backtest_id": record.id,
                "strategy_id": strategy.id,
                "vibe_run_id": fields["vibe_run_id"],
                "symbol": fields.get("symbol"),
                "metrics": metrics,
            }

            def _persist_message():
                db.add(AiChatMessage(
                    session_id=session_id,
                    role="assistant",
                    content=summary,
                    metadata_json=json.dumps(meta, ensure_ascii=False),
                ))
                db.commit()

            retry_on_lock(_persist_message, db)

        # 5. 更新任务状态
        task_manager.update_task(
            task_id,
            status="completed",
            summary=summary,
            strategy_id=strategy.id,
            strategy_name=strategy.name,
            backtest_id=record.id,
            vibe_run_id=fields["vibe_run_id"],
            symbol=fields.get("symbol"),
            metrics=metrics,
        )
        logger.info("回测完成 task=%s strategy=%s backtest=%s", task_id, strategy.id, record.id)

    except Exception as e:  # noqa: BLE001 — 后台任务需兜底所有异常
        db.rollback()
        logger.exception("回测失败 task=%s", task_id)
        if session_id:
            try:
                from models.ai_chat import AiChatMessage

                def _persist_failure():
                    db.add(AiChatMessage(
                        session_id=session_id,
                        role="assistant",
                        content=f"回测失败：{e}",
                        metadata_json=json.dumps({"error": str(e)}),
                    ))
                    db.commit()

                retry_on_lock(_persist_failure, db)
            except Exception:  # noqa: BLE001
                pass
        task_manager.update_task(task_id, status="failed", error=str(e))
    finally:
        db.close()
