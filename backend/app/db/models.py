"""业务 ORM 模型（MySQL agent_harness）。

P2 最小模型：Sessions（会话主记录）。
后续阶段按需追加：AgentMemories(P2 长期记忆) / Skills(P4) / Subagents(P6) / McpServers(P7) / AuditLogs(P5)。
"""
from datetime import datetime

from sqlalchemy import DateTime, String, Text, func
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base


class Session(Base):
    """会话主记录。ID 与 langgraph SQLite checkpoint 的 thread_id 对齐。"""

    __tablename__ = "sessions"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, comment="会话 ID（=checkpoint thread_id）")
    title: Mapped[str] = mapped_column(String(255), default="新会话", comment="会话标题")
    status: Mapped[str] = mapped_column(String(16), default="active", comment="active/archived")
    created_at: Mapped[datetime] = mapped_column(
        DateTime, server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, server_default=func.now(), onupdate=func.now(), nullable=False
    )
    note: Mapped[str] = mapped_column(Text, default="", comment="备注")
