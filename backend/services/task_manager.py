"""后台任务注册表（内存态）

AI 对话回测、手动回测等长耗时任务通过 BackgroundTasks 执行，
前端通过 GET /api/ai/tasks/{task_id} 轮询状态。
"""
import threading
import uuid
from datetime import datetime
from typing import Dict, Optional

_lock = threading.Lock()
_tasks: Dict[str, dict] = {}


def create_task(task_type: str, **meta) -> str:
    """创建任务，返回 task_id"""
    task_id = uuid.uuid4().hex[:16]
    with _lock:
        _tasks[task_id] = {
            "task_id": task_id,
            "type": task_type,
            "status": "running",  # running / completed / failed
            "created_at": datetime.now().isoformat(),
            "finished_at": None,
            "error": None,
            **meta,
        }
    return task_id


def update_task(task_id: str, status: Optional[str] = None, error: Optional[str] = None, **fields):
    with _lock:
        task = _tasks.get(task_id)
        if not task:
            return
        if status:
            task["status"] = status
        if status in ("completed", "failed"):
            task["finished_at"] = datetime.now().isoformat()
        if error is not None:
            task["error"] = error
        task.update(fields)


def get_task(task_id: str) -> Optional[dict]:
    with _lock:
        task = _tasks.get(task_id)
        return dict(task) if task else None
