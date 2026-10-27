"""Archive recall tools with fresh business-database authorization on every call."""
import json
from typing import Annotated, Literal

from langchain_core.tools import tool
from pydantic import Field
from sqlalchemy import select

from app.db.models import Session as DbSession
from app.db.session import SessionLocal
from app.agent.conversation_history import search_archive, read_archive


def authorized_sessions(session_id, user_id, scope):
    if not user_id:
        return []
    with SessionLocal() as db:
        current = db.get(DbSession, session_id)
        if current is None or current.user_id != user_id:
            return []
        if scope == "session":
            return [session_id]
        return list(db.scalars(select(DbSession.id).where(
            DbSession.user_id == user_id, DbSession.workspace_id == current.workspace_id)))


def build_history_tools(session_id, user_id):
    @tool
    async def search_history(query: Annotated[str, Field(min_length=1, max_length=200)],
                             scope: Literal["session", "workspace"] = "session",
                             limit: Annotated[int, Field(ge=1, le=20)] = 5) -> str:
        """搜索已归档的原始聊天和工具结果。默认当前会话；workspace 仅搜索本人当前工作区的会话。

        摘要缺少细节、需要确认旧约束或证据时使用。返回短片段和 message_id，再用 read_history 展开。
        查询为字面子串，支持中文；历史可能过时，用户最新指令优先。
        """
        sessions = authorized_sessions(session_id, user_id, scope)
        return json.dumps(await search_archive(sessions, query, limit), ensure_ascii=False)

    @tool
    async def read_history(message_ids: Annotated[list[str], Field(min_length=1, max_length=20)],
                           scope: Literal["session", "workspace"] = "session",
                           offset: Annotated[int, Field(ge=0, le=10000000)] = 0,
                           max_chars: Annotated[int, Field(ge=100, le=12000)] = 6000) -> str:
        """按 message_id 分页读取归档原文。可传搜索返回的 会话ID/消息ID，裸消息ID只指当前会话。

        max_chars 是本次所有结果的总字符预算；用返回的 next_offset 读取后续内容。
        跨会话读取需要 scope=workspace，且只允许本人当前工作区。未授权或不存在的 ID 不返回内容。
        """
        sessions = authorized_sessions(session_id, user_id, scope)
        return json.dumps(await read_archive(sessions, message_ids, session_id, offset, max_chars), ensure_ascii=False)

    return [search_history, read_history]
