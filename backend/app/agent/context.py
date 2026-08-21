"""上下文管理与消息历史持久化（P2，异步版）。

分层（详见 docs/P2-storage-design.md）：
- **消息历史（SQLite checkpoint）**：langgraph `AsyncSqliteSaver` 持久化每个会话的
  BaseMessage（HumanMessage/AIMessage/工具消息）与 graph 执行态，进程重启不丢，
  通过 `thread_id` 隔离多会话。
- **短期记忆（SQLite store）**：langgraph `AsyncSqliteStore`，按命名空间存键值，
  供跨节点/跨请求的临时上下文。
- **上下文压缩**：langgraph 官方 `SummarizationMiddleware`，消息数超过阈值时
  用模型把旧历史压缩成摘要，保留最近若干条。

异步注意事项：这两个对象需要一个 aiosqlite.Connection，且须在调用方相同的
event loop 中创建（FastAPI 单进程 asyncio loop 满足）。实例进程内复用。
"""
from pathlib import Path

import aiosqlite
from langchain.agents.middleware import SummarizationMiddleware
from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver
from langgraph.store.sqlite.aio import AsyncSqliteStore

from app.agent.model import build_llm
from app.core.config import settings

_saver: AsyncSqliteSaver | None = None
_store: AsyncSqliteStore | None = None
_conn: aiosqlite.Connection | None = None


async def _ensure_conn() -> aiosqlite.Connection:
    """打开 SQLite 异步连接（进程内复用，绑定首次调用所在 event loop）。"""
    global _conn
    if _conn is None:
        path = Path(settings.sqlite_path)
        path.parent.mkdir(parents=True, exist_ok=True)
        _conn = await aiosqlite.connect(str(path))
    return _conn


async def get_checkpointer() -> AsyncSqliteSaver:
    """langgraph 异步 checkpointer：消息历史 + 会话执行态持久化。"""
    global _saver
    if _saver is None:
        _saver = AsyncSqliteSaver(await _ensure_conn())
        await _saver.setup()
    return _saver


async def get_store() -> AsyncSqliteStore:
    """langgraph 异步 store：短期记忆（键值存储）。"""
    global _store
    if _store is None:
        _store = AsyncSqliteStore(await _ensure_conn())
        await _store.setup()
    return _store


def build_summary_middleware() -> SummarizationMiddleware:
    """上下文压缩中间件：消息超过 context_max_messages 时摘要，保留最近 context_keep_messages 条。"""
    return SummarizationMiddleware(
        build_llm(),
        trigger=("messages", settings.context_max_messages),
        keep=("messages", settings.context_keep_messages),
    )
