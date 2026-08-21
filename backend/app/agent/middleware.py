"""中间件框架（P5）：日志 / 审计 / 限流。

基于 langchain `AgentMiddleware` 的 hooks 实现，挂接在 create_deep_agent 的
middleware 参数里：

- **LoggingMiddleware**：结构化记录模型调用（before/after model）与工具调用，输出日志。
- **AuditMiddleware**：每次 Agent 运行（aafter_agent）把审计信息写入 MySQL `audit_logs`。
- **RateLimitMiddleware**：可配置的限流（每 thread 的消息数上限），超过则报错提示。

说明：create_deep_agent 以异步方式运行（ainvoke），故中间件实现异步 hooks（aafter_* / awrap_*）。
"""
import json
import logging
import time
from collections import defaultdict

from langchain.agents.middleware.types import AgentMiddleware

logger = logging.getLogger(__name__)


class LoggingMiddleware(AgentMiddleware):
    """结构化运行日志：记录模型调用次数与工具调用。"""

    def __init__(self):
        self._model_calls = 0
        self._tool_calls = 0
        self._tool_names = []

    @property
    def name(self) -> str:
        return "harness-logging"

    async def abefore_agent(self, state, runtime):
        self._model_calls = 0
        self._tool_calls = 0
        self._tool_names = []

    async def aafter_model(self, state, runtime):
        self._model_calls += 1

    async def awrap_tool_call(self, request, handler):
        self._tool_calls += 1
        self._tool_names.append(request.tool_call.get("name", ""))
        result = await handler(request)
        return result

    async def aafter_agent(self, state, runtime):
        logger.info(
            "[harness] run done: thread=%s model_calls=%d tool_calls=%d tools=%s delta_ms=%s",
            runtime.execution_info.thread_id,
            self._model_calls,
            self._tool_calls,
            self._tool_names,
            json.dumps({
                "model_calls": self._model_calls,
                "tool_calls": self._tool_calls,
                "tools": self._tool_names,
            }, ensure_ascii=False),
        )


class AuditMiddleware(AgentMiddleware):
    """审计中间件：Agent 运行结束后写入 MySQL audit_logs。"""

    def __init__(self):
        self._model_calls = 0
        self._tool_calls = 0
        self._tool_names = []
        self._start_ts = 0.0

    @property
    def name(self) -> str:
        return "harness-audit"

    async def abefore_agent(self, state, runtime):
        self._model_calls = 0
        self._tool_calls = 0
        self._tool_names = []
        self._start_ts = time.time()

    async def aafter_model(self, state, runtime):
        self._model_calls += 1

    async def awrap_tool_call(self, request, handler):
        self._tool_calls += 1
        self._tool_names.append(request.tool_call.get("name", ""))
        return await handler(request)

    async def aafter_agent(self, state, runtime):
        # 计算消息总量（用于统计）
        messages = _safe_messages(state)
        try:
            elapsed_ms = int((time.time() - self._start_ts) * 1000)
            detail = {
                "messages": len(messages),
                "model_calls": self._model_calls,
                "tool_calls": self._tool_calls,
                "tools": self._tool_names,
                "elapsed_ms": elapsed_ms,
            }
            await _write_audit(
                thread_id=runtime.execution_info.thread_id or "",
                run_id=runtime.execution_info.run_id or "",
                action="agent_run",
                detail=detail,
            )
        except Exception as e:  # 审计失败不影响主流程
            logger.warning("[harness] audit write failed: %s", e)


def _safe_messages(state) -> list:
    """从 state 中安全读取消息列表。"""
    try:
        return state.get("messages") or []
    except Exception:
        return []


async def _write_audit(thread_id: str, run_id: str, action: str, detail: dict) -> None:
    """将审计记录写入 MySQL（异步，后台 DB 会话）。"""
    from app.db.models import AuditLog
    from app.db.session import SessionLocal

    db = SessionLocal()
    try:
        db.add(AuditLog(
            thread_id=thread_id,
            run_id=run_id,
            action=action,
            detail=json.dumps(detail, ensure_ascii=False),
        ))
        db.commit()
    finally:
        db.close()


class RateLimitMiddleware(AgentMiddleware):
    """简单限流：每个 thread（会话）在一轮对话中的消息处理不做硬限制，
    但提供可配置的并发/频率护栏。此处实现为可选最大消息数提示。

    参数：
        max_messages: 每次运行允许的最大消息数（0 = 不限）。
    """
    def __init__(self, max_messages: int = 0):
        self._max = max_messages
        self._count = 0

    @property
    def name(self) -> str:
        return "harness-rate-limit"

    async def abefore_agent(self, state, runtime):
        self._count = len(_safe_messages(state))

    async def abefore_model(self, state, runtime):
        if self._max > 0 and self._count > self._max:
            raise RuntimeError(
                f"[harness] rate limit exceeded: messages={self._count} max={self._max}"
            )
