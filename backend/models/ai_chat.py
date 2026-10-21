"""AI 助手对话模型"""
from sqlalchemy import Column, Integer, String, DateTime, Text
from sqlalchemy.sql import func
from database import Base


class AiChatSession(Base):
    """AI 对话会话"""
    __tablename__ = "ai_chat_sessions"

    id = Column(Integer, primary_key=True, index=True)
    user_id = Column(Integer, default=1)
    title = Column(String(100), comment="会话标题（取首条消息）")
    created_at = Column(DateTime, server_default=func.now())
    updated_at = Column(DateTime, server_default=func.now(), onupdate=func.now())


class AiChatMessage(Base):
    """AI 对话消息"""
    __tablename__ = "ai_chat_messages"

    id = Column(Integer, primary_key=True, index=True)
    session_id = Column(Integer, nullable=False, index=True)
    role = Column(String(20), nullable=False, comment="user/assistant")
    content = Column(Text, nullable=False)
    metadata_json = Column(Text, comment="JSON：策略代码、回测结果等附加信息")
    created_at = Column(DateTime, server_default=func.now())
