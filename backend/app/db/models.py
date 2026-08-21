"""业务 ORM 模型（MySQL agent_harness）。

模型清单：
- Session（P2）：会话主记录
- AuditLog（P5）：Agent 运行审计
后续阶段按需追加：AgentMemories(P2 长期记忆) / Skills(P4) / Subagents(P6) / McpServers(P7)。
"""
from datetime import datetime

from sqlalchemy import DateTime, Integer, String, Text, func
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


class AuditLog(Base):
    """Agent 运行审计记录（P5）。"""

    __tablename__ = "audit_logs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    thread_id: Mapped[str] = mapped_column(String(64), index=True, default="", comment="会话/thread ID")
    run_id: Mapped[str] = mapped_column(String(64), default="", comment="run ID")
    action: Mapped[str] = mapped_column(String(32), comment="audit 事件类型，如 agent_run / tool_call / rate_limited")
    detail: Mapped[str] = mapped_column(Text, default="", comment="事件详情（JSON 字符串）")
    created_at: Mapped[datetime] = mapped_column(
        DateTime, server_default=func.now(), nullable=False, index=True
    )
