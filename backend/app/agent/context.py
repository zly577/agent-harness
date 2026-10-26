"""上下文管理与消息历史持久化（异步版）。

分层（详见 docs/P2-storage-design.md）：
- **模型执行态（SQLite checkpoint）**：langgraph `AsyncSqliteSaver` 持久化每个会话的
  BaseMessage（HumanMessage/AIMessage/工具消息）与 graph 执行态，进程重启不丢，
  通过 `thread_id` 隔离多会话。完整展示记录独立存入 conversation_messages，摘要不覆盖它。
- **短期记忆（SQLite store）**：langgraph `AsyncSqliteStore`，按命名空间存键值，
  供跨节点/跨请求的临时上下文。
- **上下文压缩**：langgraph 官方 `SummarizationMiddleware`，消息数超过阈值时
  用模型把旧历史压缩成摘要，保留最近若干条。

异步注意事项：这两个对象需要一个 aiosqlite.Connection，且须在调用方相同的
event loop 中创建（FastAPI 单进程 asyncio loop 满足）。实例进程内复用。

## ⚠️ 进程收尾必须调 `aclose()`（否则解释器退不出去）

aiosqlite 在后台线程里驱动 SQLite。连接对象不主动关掉时，**解释器退出会挂住**：
实测 `main()` 正常返回后进程 30s 都不退，只能被 kill（`_probe/exit_hang_probe.py`）。

单独看像小毛病，在 `uvicorn --reload` 下却是致命的：换代码时旧 worker 关不掉 →
reloader 一直等它 → 父进程还占着监听端口、连接也能建立，**但没人应答 HTTP** ——
表现和"服务挂了"一模一样（本项目踩过两次）。

所以 lifespan 收尾会调用本模块的 `aclose()`（见 app/main.py）。
"""
from pathlib import Path
import asyncio

import aiosqlite
from langchain.agents.middleware import SummarizationMiddleware
from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver
from langgraph.store.sqlite.aio import AsyncSqliteStore

from app.core.config import settings
import logging

logger = logging.getLogger(__name__)

_saver: AsyncSqliteSaver | None = None
_store: AsyncSqliteStore | None = None
_conn: aiosqlite.Connection | None = None
_resource_lock: asyncio.Lock | None = None
_resource_loop = None


def _storage_lock() -> asyncio.Lock:
    # The server has one loop; tests also open/close storage in sequential loops.
    global _resource_lock, _resource_loop
    loop = asyncio.get_running_loop()
    if _resource_loop is not loop:
        if _resource_lock is not None and _resource_lock.locked():
            raise RuntimeError("Storage is already initializing in another event loop")
        _resource_loop, _resource_lock = loop, asyncio.Lock()
    return _resource_lock


async def _open_conn_unlocked() -> aiosqlite.Connection:
    global _conn
    if _conn is None:
        path = Path(settings.sqlite_path)
        path.parent.mkdir(parents=True, exist_ok=True)
        opening = asyncio.ensure_future(aiosqlite.connect(str(path)))
        try:
            connection = await asyncio.shield(opening)
        except asyncio.CancelledError:
            # aiosqlite starts a worker thread during connect; do not orphan it.
            connection = await opening
            await connection.close()
            raise
        _conn = connection
    return _conn


async def _ensure_conn() -> aiosqlite.Connection:
    """打开 SQLite 异步连接（进程内复用，绑定首次调用所在 event loop）。"""
    async with _storage_lock():
        return await _open_conn_unlocked()


async def get_checkpointer() -> AsyncSqliteSaver:
    """langgraph 异步 checkpointer：消息历史 + 会话执行态持久化。"""
    global _saver
    async with _storage_lock():
        if _saver is None:
            saver = AsyncSqliteSaver(await _open_conn_unlocked())
            await saver.setup()
            _saver = saver
        return _saver


async def get_store() -> AsyncSqliteStore:
    """langgraph 异步 store：短期记忆（键值存储）。"""
    global _store
    async with _storage_lock():
        if _store is None:
            store = AsyncSqliteStore(await _open_conn_unlocked())
            await store.setup()
            _store = store
        return _store


class DisplaySafeSummarizationMiddleware(SummarizationMiddleware):
    """Archive original messages before context compression and after model steps."""

    def __init__(self, *args, session_id=None, **kwargs):
        super().__init__(*args, **kwargs)
        self.session_id = session_id

    @property
    def name(self):
        # Preserve the existing checkpoint node name for interrupted sessions.
        return "SummarizationMiddleware"

    async def _archive(self, state):
        if self.session_id:
            from app.agent.conversation_history import archive_messages
            self._ensure_message_ids(state["messages"])
            await archive_messages(self.session_id, state["messages"])

    async def abefore_agent(self, state, runtime):
        await self._archive(state)

    async def abefore_model(self, state, runtime):
        await self._archive(state)
        return await super().abefore_model(state, runtime)

    async def aafter_model(self, state, runtime):
        await self._archive(state)

    async def aafter_agent(self, state, runtime):
        await self._archive(state)


def build_summary_middleware(model=None, *, session_id=None) -> SummarizationMiddleware:
    """上下文压缩中间件：消息超过 context_max_messages 时摘要，保留最近 context_keep_messages 条。

    Args:
        model: 用来做摘要的模型。**必须传主 Agent 用的那个实例**（由
            `runtime.get_agent` 传入）—— 不传就退回 `.env` 的引导配置，
            那会导致"对话走用户选的供应商、摘要偷偷走另一个模型"，
            而且这条路径在界面上完全不可见。
    """
    if model is None:
        from app.agent.model import build_llm

        model = build_llm()
    return DisplaySafeSummarizationMiddleware(
        model,
        session_id=session_id,
        trigger=("messages", settings.context_max_messages),
        keep=("messages", settings.context_keep_messages),
    )


async def aclose() -> bool:
    """关闭 checkpointer / store 的全局连接，供**进程收尾**调用。

    幂等、尽力而为：重复调用安全；出任何异常都只记日志（收尾阶段不该再抛错）。

    Returns:
        是否真的关掉了一个连接（没有连接时返回 False）。
    """
    global _conn, _saver, _store

    async with _storage_lock():
        conn, _conn = _conn, None
        _saver = None
        _store = None
        if conn is None:
            return False
        try:
            await conn.close()
            logger.info("[context] aiosqlite 连接已关闭（进程可以正常退出了）")
        except Exception as e:  # noqa: BLE001
            logger.warning("[context] 关闭 aiosqlite 连接失败: %s", e)
        return True
