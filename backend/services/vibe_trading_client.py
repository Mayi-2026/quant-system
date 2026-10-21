"""Vibe-Trading 集成客户端

通过 subprocess 调用 vibe-trading CLI（headless 模式）执行
"自然语言 → 策略代码生成 → 回测" 流程，并解析 run 目录产物。

CLI 输出（--json）为单行 JSON：{"status","run_id","run_dir","reason"}
run 目录结构：
  config.json / run_card.md / code/signal_engine.py
  artifacts/{metrics.csv, equity.csv, trades.csv, ...}
"""
import csv
import json
import logging
import os
import subprocess
import time
from datetime import date, datetime
from pathlib import Path
from typing import Callable, Dict, List, Optional

from config import VIBE_CLI, VIBE_WORK_DIR, VIBE_RUNS_DIR, VIBE_TIMEOUT

logger = logging.getLogger(__name__)


class VibeTradingError(Exception):
    """Vibe-Trading 调用失败"""


def _snapshot_runs() -> set:
    try:
        return set(os.listdir(VIBE_RUNS_DIR))
    except FileNotFoundError:
        return set()


def _detect_newest_run(before: set) -> Optional[str]:
    """检测本轮新增的最新 run 目录（不要求完成）"""
    try:
        new_dirs = [d for d in os.listdir(VIBE_RUNS_DIR) if d not in before]
    except FileNotFoundError:
        return None
    if not new_dirs:
        return None
    return sorted(new_dirs, reverse=True)[0]


def _detect_finished_run(before: set) -> Optional[str]:
    """检测本轮新增 run 目录是否已完成回测（state=success 且产物齐备）"""
    try:
        new_dirs = [d for d in os.listdir(VIBE_RUNS_DIR) if d not in before]
    except FileNotFoundError:
        return None
    for d in sorted(new_dirs, reverse=True):
        run_dir = Path(VIBE_RUNS_DIR) / d
        state = run_dir / "state.json"
        if not state.exists():
            continue
        try:
            if json.loads(state.read_text(encoding="utf-8")).get("status") != "success":
                continue
        except (json.JSONDecodeError, OSError):
            continue
        # 产物齐备才算完成
        if (run_dir / "artifacts" / "metrics.csv").exists() and (run_dir / "artifacts" / "equity.csv").exists():
            return d
    return None


def _get_run_progress(run_id: str) -> str:
    """检测 run 目录的当前进度阶段"""
    run_dir = Path(VIBE_RUNS_DIR) / run_id
    if not run_dir.exists():
        return "等待 CLI 创建 run 目录..."

    # 检查 state.json
    state_file = run_dir / "state.json"
    state = None
    if state_file.exists():
        try:
            state = json.loads(state_file.read_text(encoding="utf-8")).get("status")
        except (json.JSONDecodeError, OSError):
            pass

    # 检查产物
    has_code = (run_dir / "code" / "signal_engine.py").exists()
    has_metrics = (run_dir / "artifacts" / "metrics.csv").exists()
    has_equity = (run_dir / "artifacts" / "equity.csv").exists()
    has_trades = (run_dir / "artifacts" / "trades.csv").exists()
    has_run_card = (run_dir / "run_card.md").exists()

    if has_metrics and has_equity:
        return "回测完成，正在解析产物..."
    if has_trades and has_equity:
        return "正在生成回测报告..."
    if has_equity:
        return "正在解析交易明细..."
    if has_metrics:
        return "正在生成净值曲线..."
    if has_code and state == "success":
        return "策略代码已生成，正在执行回测..."
    if has_code:
        return "策略代码已生成，正在等待回测引擎..."
    if state == "running" or state is None:
        return "AI Agent 正在分析策略并生成代码..."
    return "等待 CLI 响应..."


def run_backtest(prompt: str, timeout: int = VIBE_TIMEOUT, progress_callback: Optional[Callable[[str], None]] = None) -> Dict:
    """同步执行一次 vibe-trading 回测（阻塞，供后台线程调用）

    策略：后台轮询 run 目录，一旦 state.json=success 且 metrics/equity 产物
    落盘即提前终止 CLI（agent 的后续评审阶段可能耗时 30 分钟，与结果无关）。

    progress_callback: 可选回调函数，接收进度描述字符串

    返回 {"status", "run_id", "run_dir", "reason"}
    """
    def _report(msg: str):
        if progress_callback:
            progress_callback(msg)
        logger.info("[vibe-backtest] %s", msg)

    _report("正在启动 Vibe-Trading CLI...")
    before = _snapshot_runs()
    try:
        proc = subprocess.Popen(
            [VIBE_CLI, "run", "-p", prompt, "--json", "--no-rich"],
            cwd=VIBE_WORK_DIR,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
    except OSError as e:
        raise VibeTradingError(f"无法启动 vibe-trading CLI: {e}")

    _report("CLI 已启动，PID: %d" % proc.pid)
    start = time.time()
    payload = None
    last_progress_msg = ""
    while True:
        ret = proc.poll()
        if ret is not None:
            # 进程自然结束：从 stdout 提取最后一行合法 JSON
            out = proc.stdout.read() or ""
            for line in reversed(out.strip().splitlines()):
                line = line.strip()
                if line.startswith("{") and line.endswith("}"):
                    try:
                        payload = json.loads(line)
                        break
                    except json.JSONDecodeError:
                        continue
            break
        if time.time() - start > timeout:
            proc.kill()
            raise VibeTradingError(f"回测超时（>{timeout}s）")

        # 检测新 run 目录并报告进度
        new_run = _detect_newest_run(before)
        if new_run:
            progress_msg = _get_run_progress(new_run)
            if progress_msg != last_progress_msg:
                _report(progress_msg)
                last_progress_msg = progress_msg
        else:
            elapsed = int(time.time() - start)
            if elapsed % 15 == 0 and elapsed > 0:
                _report(f"CLI 运行中... 已等待 {elapsed} 秒")

        run_id = _detect_finished_run(before)
        if run_id:
            _report("回测产物已生成，正在终止 CLI 评审阶段...")
            time.sleep(2)  # 等待文件句柄释放
            proc.terminate()
            try:
                proc.wait(timeout=15)
            except subprocess.TimeoutExpired:
                proc.kill()
            _report("回测完成")
            return {
                "status": "success",
                "run_id": run_id,
                "run_dir": str(Path(VIBE_RUNS_DIR) / run_id),
            }
        time.sleep(3)

    if payload is None or payload.get("status") != "success" or not payload.get("run_dir"):
        try:
            stderr = (proc.stderr.read() or "")[-800:]
        except Exception:  # noqa: BLE001
            stderr = ""
        reason = (payload or {}).get("reason") if payload else None
        raise VibeTradingError(reason or f"回测失败：status={(payload or {}).get('status') if payload else 'no-output'}。stderr: {stderr}")

    return payload


def _parse_metrics(run_dir: Path) -> Dict:
    """解析 artifacts/metrics.csv（单行数据）"""
    path = run_dir / "artifacts" / "metrics.csv"
    if not path.exists():
        return {}
    with open(path, newline="", encoding="utf-8") as f:
        rows = list(csv.DictReader(f))
    if not rows:
        return {}
    raw = rows[0]

    def _f(key: str) -> Optional[float]:
        try:
            v = raw.get(key)
            return float(v) if v not in (None, "") else None
        except (TypeError, ValueError):
            return None

    def _i(key: str) -> Optional[int]:
        v = _f(key)
        return int(v) if v is not None else None

    return {
        "final_value": _f("final_value"),
        "total_return": _f("total_return"),
        "annual_return": _f("annual_return"),
        "max_drawdown": _f("max_drawdown"),
        "sharpe_ratio": _f("sharpe"),
        "calmar_ratio": _f("calmar"),
        "sortino_ratio": _f("sortino"),
        "win_rate": _f("win_rate"),
        "profit_loss_ratio": _f("profit_loss_ratio"),
        "trade_count": _i("trade_count"),
        "avg_holding_days": _f("avg_holding_days"),
        "max_consecutive_loss": _i("max_consecutive_loss"),
        "benchmark_return": _f("benchmark_return"),
        "excess_return": _f("excess_return"),
        "beta": _f("benchmark_beta"),
    }


def _parse_config(run_dir: Path) -> Dict:
    """解析 config.json（标的、区间、初始资金）"""
    path = run_dir / "config.json"
    if not path.exists():
        return {}
    try:
        cfg = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return {}
    codes = cfg.get("codes") or []
    return {
        "symbol": codes[0] if codes else None,
        "start_date": cfg.get("evaluation_start_date") or cfg.get("start_date"),
        "end_date": cfg.get("end_date"),
        "initial_cash": cfg.get("initial_cash", 1000000),
        "engine_type": cfg.get("engine", "daily"),
    }


def _parse_equity(run_dir: Path, max_points: int = 400) -> List[Dict]:
    """解析净值曲线并降采样"""
    path = run_dir / "artifacts" / "equity.csv"
    if not path.exists():
        return []
    rows = []
    with open(path, newline="", encoding="utf-8") as f:
        for row in csv.DictReader(f):
            try:
                rows.append({
                    "date": row["timestamp"],
                    "equity": float(row["equity"]),
                    "benchmark": float(row["benchmark_equity"]),
                    "drawdown": float(row["drawdown"]),
                })
            except (KeyError, ValueError):
                continue
    if len(rows) <= max_points:
        return rows
    step = len(rows) / max_points
    return [rows[int(i * step)] for i in range(max_points)] + [rows[-1]]


def _parse_trades(run_dir: Path) -> List[Dict]:
    """解析交易明细"""
    path = run_dir / "artifacts" / "trades.csv"
    if not path.exists():
        return []
    trades = []
    with open(path, newline="", encoding="utf-8") as f:
        for row in csv.DictReader(f):
            try:
                trades.append({
                    "date": row["timestamp"],
                    "code": row.get("code", ""),
                    "side": row.get("side", ""),
                    "price": float(row["price"]),
                    "qty": float(row["qty"]),
                    "pnl": float(row["pnl"]),
                    "return_pct": float(row["return_pct"]),
                    "holding_days": float(row.get("holding_days", 0)),
                })
            except (KeyError, ValueError):
                continue
    return trades


def load_run_artifacts(run_dir: str) -> Dict:
    """加载一次 run 的全部产物（指标 / 曲线 / 交易 / 代码 / 报告）"""
    d = Path(run_dir)
    if not d.exists():
        raise VibeTradingError(f"run 目录不存在: {run_dir}")

    code_path = d / "code" / "signal_engine.py"
    run_card_path = d / "run_card.md"
    equity_csv = d / "artifacts" / "equity.csv"
    trades_csv = d / "artifacts" / "trades.csv"

    return {
        "config": _parse_config(d),
        "metrics": _parse_metrics(d),
        "equity": _parse_equity(d),
        "trades": _parse_trades(d),
        "strategy_code": code_path.read_text(encoding="utf-8") if code_path.exists() else None,
        "run_card": run_card_path.read_text(encoding="utf-8") if run_card_path.exists() else None,
        "paths": {
            "run_dir": str(d),
            "equity_csv": str(equity_csv) if equity_csv.exists() else None,
            "trades_csv": str(trades_csv) if trades_csv.exists() else None,
            "run_card": str(run_card_path) if run_card_path.exists() else None,
            "strategy_code": str(code_path) if code_path.exists() else None,
        },
    }


def _to_date(s) -> Optional[date]:
    if not s:
        return None
    try:
        return datetime.strptime(str(s)[:10], "%Y-%m-%d").date()
    except ValueError:
        return None


def build_backtest_record_fields(run_dir: str) -> Dict:
    """从 run 产物中提取写入 backtest_records / strategies 的字段"""
    art = load_run_artifacts(run_dir)
    cfg, metrics = art["config"], art["metrics"]
    return {
        "vibe_run_id": Path(run_dir).name,
        "run_dir": run_dir,
        "symbol": cfg.get("symbol"),
        "start_date": _to_date(cfg.get("start_date")),
        "end_date": _to_date(cfg.get("end_date")),
        "initial_cash": cfg.get("initial_cash"),
        "engine_type": cfg.get("engine_type"),
        "strategy_code": art["strategy_code"],
        "metrics": metrics,
        "paths": art["paths"],
        "equity": art["equity"],
        "trades": art["trades"],
        "run_card": art["run_card"],
    }


vibe_client = type("VibeTradingClient", (), {
    "run_backtest": staticmethod(run_backtest),
    "load_run_artifacts": staticmethod(load_run_artifacts),
    "build_backtest_record_fields": staticmethod(build_backtest_record_fields),
    "runs_dir": VIBE_RUNS_DIR,
})()
