"""AI 策略助手 API（/api/ai/*）"""
import json
from fastapi import APIRouter, Depends, HTTPException, BackgroundTasks
from pydantic import BaseModel
from sqlalchemy.orm import Session
from typing import List, Optional

from database import get_db
from models.ai_chat import AiChatSession, AiChatMessage
from services import task_manager
from services.backtest_runner import execute_backtest

router = APIRouter(prefix="/api/ai", tags=["AI 助手"])


class ChatRequest(BaseModel):
    session_id: Optional[int] = None
    message: str


def _msg_dict(m: AiChatMessage) -> dict:
    meta = None
    if m.metadata_json:
        try:
            meta = json.loads(m.metadata_json)
        except json.JSONDecodeError:
            meta = None
    return {
        "id": m.id,
        "session_id": m.session_id,
        "role": m.role,
        "content": m.content,
        "metadata": meta,
        "created_at": m.created_at.isoformat() if m.created_at else None,
    }


@router.post("/chat")
def chat(req: ChatRequest, background_tasks: BackgroundTasks, db: Session = Depends(get_db)):
    """发送消息：创建后台回测任务并立即返回 task_id，前端轮询 /tasks/{id}"""
    message = req.message.strip()
    if not message:
        raise HTTPException(status_code=400, detail="消息不能为空")

    # 1. 获取或创建会话
    if req.session_id:
        session = db.query(AiChatSession).filter(AiChatSession.id == req.session_id).first()
        if not session:
            raise HTTPException(status_code=404, detail="会话不存在")
    else:
        session = AiChatSession(title=message[:30])
        db.add(session)
        db.commit()
        db.refresh(session)

    # 2. 保存用户消息
    db.add(AiChatMessage(session_id=session.id, role="user", content=message))
    db.commit()

    # 3. 创建后台任务
    task_id = task_manager.create_task("ai_chat", session_id=session.id, message=message)
    background_tasks.add_task(
        execute_backtest,
        task_id=task_id,
        prompt=message,
        strategy_id=None,
        session_id=session.id,
        user_message=message,
    )

    return {"session_id": session.id, "task_id": task_id}


@router.get("/tasks/{task_id}")
def get_task(task_id: str):
    """轮询任务状态：running / completed（含 summary、backtest_id、metrics）/ failed"""
    task = task_manager.get_task(task_id)
    if not task:
        raise HTTPException(status_code=404, detail="任务不存在")
    return task


@router.get("/sessions")
def list_sessions(db: Session = Depends(get_db)):
    """会话列表"""
    sessions = (
        db.query(AiChatSession)
        .order_by(AiChatSession.updated_at.desc())
        .limit(50)
        .all()
    )
    return [
        {
            "id": s.id,
            "title": s.title,
            "created_at": s.created_at.isoformat() if s.created_at else None,
            "updated_at": s.updated_at.isoformat() if s.updated_at else None,
        }
        for s in sessions
    ]


@router.get("/sessions/{session_id}")
def get_session_detail(session_id: int, db: Session = Depends(get_db)):
    """会话详情（含全部消息）"""
    session = db.query(AiChatSession).filter(AiChatSession.id == session_id).first()
    if not session:
        raise HTTPException(status_code=404, detail="会话不存在")
    messages = (
        db.query(AiChatMessage)
        .filter(AiChatMessage.session_id == session_id)
        .order_by(AiChatMessage.id.asc())
        .all()
    )
    return {"session": {"id": session.id, "title": session.title}, "messages": [_msg_dict(m) for m in messages]}
