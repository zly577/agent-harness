"""上下文管理与消息历史持久化（异步版）。

分层（详见 docs/context-management.md）：
- **模型执行态（SQLite checkpoint）**：langgraph `AsyncSqliteSaver` 持久化每个会话的
  BaseMessage（HumanMessage/AIMessage/工具消息）与 graph 执行态，进程重启不丢，
  通过 `thread_id` 隔离多会话。完整展示记录独立存入 conversation_messages，摘要不覆盖它。
- **短期记忆（SQLite store）**：langgraph `AsyncSqliteStore`，按命名空间存键值，
  供跨节点/跨请求的临时上下文。
- **上下文压缩**：DeepAgents 增强摘要层，按模型输入预算清理、外置和摘要；
  模型视图独立于原始消息，展示归档独立保存。

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
import time
from contextvars import ContextVar
from typing import Annotated, NotRequired

import aiosqlite
from langchain.agents.middleware.types import AgentMiddleware, ExtendedModelResponse, PrivateStateAttr
from langgraph.types import Command
from deepagents.middleware.summarization import (
    SummarizationMiddleware,
    SummarizationState,
)
from app.agent.context_budget import estimate_tokens, resolve_budget
from app.agent.context_cleaner import ContextCleaner
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


class ConversationArchiveMiddleware(AgentMiddleware):
    """Persist display history independently of the model's compressed view."""

    def __init__(self, session_id):
        self.session_id = session_id
        self._archived_ids = set()

    async def _archive(self, state):
        if self.session_id:
            from app.agent.conversation_history import archive_messages
            messages = [m for m in state["messages"] if not m.id or m.id not in self._archived_ids]
            await archive_messages(self.session_id, messages)
            self._archived_ids.update(m.id for m in messages if m.id)

    async def abefore_agent(self, state, runtime):
        await self._archive(state)

    async def abefore_model(self, state, runtime):
        await self._archive(state)

    async def aafter_model(self, state, runtime):
        await self._archive(state)

    async def aafter_agent(self, state, runtime):
        await self._archive(state)


class ContextState(SummarizationState):
    context_usage: NotRequired[Annotated[dict, PrivateStateAttr]]


class EnhancedSummarizationMiddleware(SummarizationMiddleware):
    """DeepAgents compression with legacy checkpoint node compatibility.

    Old graphs could pause at these hooks. Keep the node names while compression
    itself uses DeepAgents' non-mutating request wrapper.
    """

    state_schema = ContextState

    def __init__(self, *args, preferences=None, request_projectors=(), session_id=None, use_token_budget=False, **kwargs):
        super().__init__(*args, **kwargs)
        self.preferences = preferences or {}
        self.request_projectors = request_projectors
        self._request_context = ContextVar(f"context-{id(self)}", default=None)
        self.cleaner = ContextCleaner(self._backend, session_id, settings.context_tool_result_tokens)
        self.use_token_budget = use_token_budget

    def _should_summarize(self, messages, total_tokens):
        current = self._request_context.get()
        if self.use_token_budget and current:
            return total_tokens >= current["budget"].trigger
        return super()._should_summarize(messages, total_tokens)

    def _determine_cutoff_index(self, messages):
        current = self._request_context.get()
        if not self.use_token_budget or not current:
            return super()._determine_cutoff_index(messages)
        # Account for the fixed prompt and observed tokenizer calibration when
        # reserving a recent suffix. Always retain the newest tool transaction.
        available = current["budget"].input_limit - current["fixed_tokens"] - 2048
        target = max(1, min(current["budget"].keep, available // 2))
        left, right = 0, len(messages)
        while left < right:
            middle = (left + right) // 2
            if self.token_counter(messages[middle:]) * current["calibration"] <= target:
                right = middle
            else:
                left = middle + 1
        return self._lc_helper._find_safe_cutoff_point(messages, min(left, max(0, len(messages) - 1)))

    async def _acreate_summary(self, messages):
        started = time.monotonic()
        current = self._request_context.get()
        if current is not None:
            current["memory_compacted"] = True
        try:
            return await super()._acreate_summary(messages)
        finally:
            current = self._request_context.get()
            if current is not None:
                current["summary_duration_ms"] = round((time.monotonic() - started) * 1000)

    def _count_tokens(self, messages, system_message, tools):
        current = self._request_context.get()
        if current:
            system_message = current["system_message"]
            tools = current["tools"]
        result = super()._count_tokens(messages, system_message, tools)
        return int(result * (current["calibration"] if current else 1))

    def _project_request(self, request):
        projected = request
        # These inner middleware only format copies of the request. Include their
        # memory/skill prompt fragments in the budget without injecting them twice.
        for middleware in self.request_projectors:
            if middleware.name == "MemoryMiddleware" and "memory_contents" not in request.state:
                continue
            if middleware.name == "SkillsMiddleware" and "skills_metadata" not in request.state:
                continue
            required = {getattr(tool, "name", None) for tool in getattr(middleware, "tools", [])}
            available = {getattr(tool, "name", None) if not isinstance(tool, dict) else tool.get("name", tool.get("function", {}).get("name")) for tool in request.tools}
            if required - available:
                continue
            projected = middleware.modify_request(projected)
        return projected

    async def awrap_model_call(self, request, handler):
        budget = resolve_budget(request.model, self.preferences, request.model_settings)
        projected = self._project_request(request)
        prior = request.state.get("context_usage") or {}
        current = {"system_message": projected.system_message,
                   "tools": projected.tools,
                   "budget": budget,
                   "calibration": max(1.0, min(4.0, prior.get("calibration", 1.0)))}
        token = self._request_context.set(current)
        started = time.monotonic()
        try:
            current["fixed_tokens"] = self._count_tokens([], request.system_message, request.tools)
            before = self._count_tokens(self._get_effective_messages(request), request.system_message, request.tools)
            event = request.state.get("_summarization_event") or {}
            cleaned, cleaning = await self.cleaner.clean(request.messages, pressure=before >= budget.trigger,
                                                         cutoff=event.get("cutoff_index", 0))
            request = request.override(messages=cleaned)
            sent = {}
            memory_update = {}

            async def measured(next_request):
                from app.agent.workspace_memory import prepare_memory_request
                next_request, rebased = prepare_memory_request(next_request, compacted=current.get("memory_compacted", False))
                memory_update.update(rebased)
                projected = self._project_request(next_request)
                current.update(system_message=projected.system_message, tools=projected.tools)
                sent["estimate"] = self._count_tokens(next_request.messages, next_request.system_message, next_request.tools)
                if sent["estimate"] > budget.input_limit:
                    raise ValueError("上下文仍超过输入预算：请缩小工具定义、常驻记忆或单条输入，或配置正确的模型窗口")
                return await handler(next_request)

            response = await super().awrap_model_call(request, measured)
            result = response.model_response if isinstance(response, ExtendedModelResponse) else response
            actual = next((m.usage_metadata.get("input_tokens", 0) for m in reversed(result.result)
                           if getattr(m, "usage_metadata", None)), 0)
            update = dict(response.command.update or {}) if isinstance(response, ExtendedModelResponse) and response.command else {}
            compacted = "_summarization_event" in update
            update.update(memory_update)
            raw_estimate = max(1, sent["estimate"] / current["calibration"])
            metrics = {"window": budget.window, "source": budget.source, "input_limit": budget.input_limit,
                       "trigger": budget.trigger, "estimated_before": before,
                       "estimated_input": sent["estimate"], "actual_input": actual,
                       "calibration": max(current["calibration"], min(4.0, actual / raw_estimate)),
                       "compactions": prior.get("compactions", 0) + int(compacted),
                       "compacted": compacted, "duration_ms": round((time.monotonic() - started) * 1000)}
            metrics["summary_duration_ms"] = current.get("summary_duration_ms", 0)
            metrics.update(cleaning)
            update["context_usage"] = metrics
            if compacted:
                logger.info("[context] compaction %s", metrics)
            return ExtendedModelResponse(model_response=result, command=Command(update=update))
        finally:
            self._request_context.reset(token)

    @property
    def name(self):
        return "SummarizationMiddleware"

    @property
    def trigger(self):
        return self._lc_helper.trigger

    @property
    def keep(self):
        return self._lc_helper.keep

    async def abefore_agent(self, state, runtime):
        return None

    async def abefore_model(self, state, runtime):
        return None

    async def aafter_model(self, state, runtime):
        return None

    async def aafter_agent(self, state, runtime):
        return None


def build_summary_middleware(model=None, *, backend, session_id=None, preferences=None, request_projectors=()) -> SummarizationMiddleware:
    """Build the recoverable, model-aware context policy for this session.

    Args:
        model: 用来做摘要的模型。**必须传主 Agent 用的那个实例**（由
            `runtime.get_agent` 传入）—— 不传就退回 `.env` 的引导配置，
            那会导致"对话走用户选的供应商、摘要偷偷走另一个模型"，
            而且这条路径在界面上完全不可见。
    """
    if model is None:
        from app.agent.model import build_llm

        model = build_llm()
    values = preferences or {}
    budget = resolve_budget(model, values)
    return EnhancedSummarizationMiddleware(
        model,
        backend=backend,
        preferences=values,
        use_token_budget=True,
        session_id=session_id,
        request_projectors=request_projectors,
        trigger=("tokens", budget.trigger),
        keep=("tokens", budget.keep),
        token_counter=estimate_tokens,
        truncate_args_settings={"trigger": ("tokens", budget.trigger * 3 // 4),
                                "keep": ("tokens", budget.keep), "max_length": 4000},
        trim_tokens_to_summarize=None,
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
