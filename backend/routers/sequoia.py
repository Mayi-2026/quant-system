"""Sequoia-X 选股系统菜单 API。"""

import json
import subprocess
import threading
from datetime import datetime
from pathlib import Path

from fastapi import APIRouter, BackgroundTasks

from config import SEQUOIA_DIR, SEQUOIA_PYTHON, SEQUOIA_RUN_TIMEOUT
from services import sequoia_sync

router = APIRouter(prefix="/api/sequoia", tags=["Sequoia选股"])

# 选股运行状态（后端单进程，内存态即可；前端通过 /run/status 轮询）
_run_lock = threading.Lock()
_run_state = {
    "running": False,
    "status": "idle",  # idle | running | success | error
    "started_at": None,
    "finished_at": None,
    "results": None,
    "message": None,
}


def _now() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


@router.get("/status")
def get_status():
    """两侧库规模与日期范围。"""
    return sequoia_sync.status()


@router.post("/sync")
def sync(mode: str = "recent", days: int = 30):
    """把 quant.db 行情同步一份到 sequoia_v2.db。

    mode=recent 增量最近 N 天（默认，每日收盘后调用）；mode=all 全量。
    """
    if mode == "all":
        return sequoia_sync.sync_all()
    return sequoia_sync.sync_recent(days=days)


def _parse_result(stdout: str):
    """从脚本 stdout 中解析 `RESULT:<json>` 行，找不到返回 None。"""
    for line in stdout.splitlines():
        if line.startswith("RESULT:"):
            try:
                return json.loads(line[len("RESULT:"):])
            except json.JSONDecodeError:
                continue
    return None


def _exec_run() -> None:
    """后台执行选股子进程，结果写回 _run_state。"""
    script = Path(SEQUOIA_DIR) / "run_strategies.py"
    try:
        proc = subprocess.run(
            [SEQUOIA_PYTHON, str(script)],
            cwd=SEQUOIA_DIR,
            capture_output=True,
            text=True,
            timeout=SEQUOIA_RUN_TIMEOUT,
        )
    except subprocess.TimeoutExpired:
        with _run_lock:
            _run_state.update(
                running=False, status="error", finished_at=_now(),
                message=f"选股超时（>{SEQUOIA_RUN_TIMEOUT}s）", results=None,
            )
        return
    except Exception as exc:
        with _run_lock:
            _run_state.update(
                running=False, status="error", finished_at=_now(),
                message=str(exc), results=None,
            )
        return

    results = _parse_result(proc.stdout)
    with _run_lock:
        if results is None:
            tail = (proc.stderr or proc.stdout or "")[-600:]
            _run_state.update(
                running=False, status="error", finished_at=_now(),
                message="选股脚本未返回结果", results=None, _detail=tail,
            )
        else:
            _run_state.update(
                running=False, status="success", finished_at=_now(),
                results=results, message=None,
            )
    return


@router.post("/run")
def run_strategies(background_tasks: BackgroundTasks):
    """在后台启动 Sequoia-X 选股，立即返回；用 GET /run/status 轮询结果。

    完整跑一轮约 8~11 分钟，改为后台任务避免 Web 请求超时。
    """
    script = Path(SEQUOIA_DIR) / "run_strategies.py"
    if not script.exists():
        return {"status": "error", "message": f"未找到脚本: {script}"}

    with _run_lock:
        if _run_state["running"]:
            return {"status": "running", "started_at": _run_state["started_at"]}
        # 预标记为 running，防止并发重复启动
        _run_state.update(
            running=True, status="running", started_at=_now(),
            finished_at=None, results=None, message=None,
        )
        started_at = _run_state["started_at"]

    background_tasks.add_task(_exec_run)
    return {"status": "started", "started_at": started_at}


@router.get("/run/status")
def get_run_status():
    """查询选股运行状态与结果。"""
    with _run_lock:
        return dict(_run_state)