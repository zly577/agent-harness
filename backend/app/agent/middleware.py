"""中间件框架（P5）：日志 / 审计。

基于 langchain `AgentMiddleware` 的 hooks 实现，挂接在 create_deep_agent 的
middleware 参数里：

- **LoggingMiddleware**：结构化记录模型调用（before/after model）与工具调用，输出日志。
- **AuditMiddleware**：每次 Agent 运行（aafter_agent）把审计信息写入 MySQL `audit_logs`。

说明：create_deep_agent 以异步方式运行（ainvoke），故中间件实现异步 hooks（aafter_* / awrap_*）。

限流/次数限制不再自研：改用 langchain 现成的 `ToolCallLimitMiddleware`
（见 runtime.get_agent 的 middlewares 列表），它按工具名统计调用次数并支持
continue(降级) / error / end 三种处置方式。
"""
import json
import logging
import re
import threading
import time
from collections import defaultdict
from dataclasses import dataclass

from langchain.agents.middleware.types import AgentMiddleware

logger = logging.getLogger(__name__)


# ============================================================================
# 工具连续失败 → 熔断降级（换方法）
#
# 注意：这里**不使用 graph state** 记计数。原因有两条，都是踩过的坑：
#   1. 同一批并行工具调用看到的是同一个 state 快照（`ToolCallRequest.state`），
#      靠 state 计数拦不住"一轮里并行发 7 个同名调用"；
#   2. 若让每个调用返回 `Command(update={"tool_failure_count": ...})`，同一个 superstep
#      内同一个 key 被更新多次 → `InvalidUpdateError: Can receive only one value per step`。
# 所以权威状态放在进程内的 `ToolBreakerRegistry`（按 会话+工具 隔离），state 不参与。
# ============================================================================


def _is_tool_failure(result) -> bool:
    """判断一次工具返回是否算"失败"（异常由调用方单独处理）。

    覆盖本 harness 里会出现的几种形状：
    - `ToolMessage.status == "error"`（deepagents 的 `_tool_error`、HITL 的 reject 都走这个）；
    - dict 里带**非空** `error` 字段（`FileUploadResponse` / `FileDownloadResponse` /
      `FsResult.error` 的形状）；空字符串按成功算；
    - 文本内容以 `Error:` 开头（有些工具直接返回文本错误而不是抛异常）。
    """
    if getattr(result, "status", None) == "error":
        return True

    content = getattr(result, "content", result)
    if isinstance(content, dict):
        return bool(content.get("error"))
    if isinstance(content, str) and content.lstrip().startswith("Error:"):
        return True
    if isinstance(result, dict):
        return bool(result.get("error"))
    return False


def _build_switch_message(tool_name: str, limit: int, cooldown: int) -> str:
    """给模型的"换方法"指令（作为工具结果回给模型）。"""
    return (
        f"工具 `{tool_name}` 连续失败 {limit} 次，已进入 {cooldown} 秒冷却期，"
        f"本次调用被跳过、工具未执行。\n"
        f"请现在改用其他方法或工具完成任务，不要马上再试 `{tool_name}`。\n"
        f"冷却结束后第一次调用会被当作探测放行，成功即自动恢复；"
        f"如果用户要求立刻重试，可以先调用 `reset_tool_breakers` 重置熔断再试。"
    )


@dataclass
class _Breaker:
    """单个工具的熔断状态。"""

    consecutive_failures: int = 0
    cooling: bool = False
    #: 冷却截止时间（time.monotonic()）
    cooldown_until: float = 0.0
    #: 本次冷却是否已放过探测（保证同一批并行调用只放一个）
    probe_used: bool = False
    #: 正在执行中的调用数（"占位"）：并行批里先到先占，防止一波全冲过去
    in_flight: int = 0
    #: 上一次放行探测的时间；用于"每次探测之间也要隔一个冷却期"，避免 COOLDOWN=0 时探测无限重复
    last_probe_at: float = 0.0

    def is_cooling(self, now: float) -> bool:
        return self.cooling and now < self.cooldown_until

    def probe_allowed(self, now: float, interval: float) -> bool:
        """冷却期已过、且距上次探测已隔一个间隔、且本次冷却还没放过探测。"""
        return (
            self.cooling
            and now >= self.cooldown_until
            and not self.probe_used
            and (now - self.last_probe_at) >= max(interval, 1.0)
        )


class ToolBreakerRegistry:
    """进程内的工具熔断表：`"{thread_id}:{tool}" → _Breaker`。

    为什么放进程内、而不是 state：
    1. 熔断要"冷却计时 + 半开探测"，时间语义放 state 里既难写又要跨轮比较；
    2. **更关键**：同一批并行调用看到的 state 是进入 tools 节点时的快照
       （`ToolCallRequest.state`，`Command.update` 要等节点结束才合并），
       用进程内计数才能让"一轮里并行发 7 个同名调用"也受上限约束。
    进程重启后表清空 = 所有工具重新获得机会，这也是期望行为。
    """

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._breakers: dict[str, _Breaker] = {}

    @staticmethod
    def _key(thread_id: str, tool: str) -> str:
        return f"{thread_id}:{tool}"

    def get(self, thread_id: str, tool: str) -> _Breaker:
        with self._lock:
            return self._breakers.setdefault(self._key(thread_id, tool), _Breaker())

    def note_success(self, thread_id: str, tool: str) -> None:
        """成功一次 → 计数清零、熔断解除（"连续"的语义就在这）。"""
        with self._lock:
            b = self._breakers.get(self._key(thread_id, tool))
            if b is None:
                return
            b.consecutive_failures = 0
            b.cooling = False
            b.cooldown_until = 0.0
            b.probe_used = False

    def reset_all(self, thread_id: str | None = None) -> int:
        """清空熔断状态；返回清掉的条目数（D 方案的手动恢复入口）。"""
        with self._lock:
            if thread_id is None:
                n = len(self._breakers)
                self._breakers.clear()
                return n
            prefix = f"{thread_id}:"
            keys = [k for k in self._breakers if k.startswith(prefix)]
            for k in keys:
                self._breakers.pop(k, None)
            return len(keys)

    def start_run(self, thread_id: str) -> None:
        """新一轮对话开始：把该会话所有工具的"连续失败预算"重置。

        为什么每轮重置：一轮里的连续失败基本等于"模型在死磕同一个工具"，那才该熔断；
        跨轮累加会把不同任务里的偶发失败算到一个账上，导致工具被永久判死。
        熔断中的冷却与探测标记**不重置**（那是正在生效的冷却窗口，见 `is_cooling`）。
        """
        with self._lock:
            prefix = f"{thread_id}:"
            for k, b in self._breakers.items():
                if k.startswith(prefix):
                    b.consecutive_failures = 0

    def cooling_tools(self, thread_id: str) -> dict[str, dict]:
        """当前处于冷却中的工具（诊断用）。"""
        now = time.monotonic()
        with self._lock:
            prefix = f"{thread_id}:"
            return {
                k[len(prefix):]: {
                    "consecutive_failures": b.consecutive_failures,
                    "cooldown_remaining": max(0, int(b.cooldown_until - now)),
                }
                for k, b in self._breakers.items()
                if k.startswith(prefix) and b.is_cooling(now)
            }


#: 进程内单例
breaker_registry = ToolBreakerRegistry()


class ConsecutiveToolFailureMiddleware(AgentMiddleware):
    """工具熔断降级：**对所有工具各自独立生效**（每个工具一个熔断器）。

    状态机：

        closed（正常）
          └─ 连续失败达阈值（默认 5）→ open（冷却 N 秒，期间调用被跳过、不执行）
                └─ 冷却到期 → half-open：放行**一次**探测
                      ├─ 成功 → closed（完全恢复、计数清零）   ← B：自愈
                      └─ 失败 → 重新 open（再等 N 秒）

    外加 D：模型可调用 `reset_tool_breakers` 工具立刻清空所有熔断（用户要求重试时用）。

    与 langchain 自带 `ToolCallLimitMiddleware` 的区别：那个数**调用次数**（成功也算、
    成功不清零）；本类数**连续失败次数**，成功一次即清零。

    降级是"让模型换方法"：短路时回一条说明（`status="success"`，避免被"见错就重试"
    的中间件再抓一次），让模型自主改用别的工具或办法。

    实现要点：熔断状态放在**进程内 registry**，不写 graph state —— 详见本文件顶部
    "工具连续失败 → 熔断降级"那段注释（state 快照看不到同批并发，且多写同一 key
    会触发 `InvalidUpdateError`）。
    """

    def __init__(
        self,
        *,
        tool_name: str | None = None,
        max_consecutive_failures: int = 5,
        cooldown_seconds: int = 60,
        recovery_tool_name: str | None = "reset_tool_breakers",
        registry: ToolBreakerRegistry | None = None,
    ) -> None:
        super().__init__()
        if max_consecutive_failures < 1:
            msg = "max_consecutive_failures 必须 >= 1"
            raise ValueError(msg)
        self.tool_name = tool_name  # None = 所有工具各自独立熔断（默认）
        self.max_consecutive_failures = max_consecutive_failures
        self.cooldown_seconds = max(0, cooldown_seconds)
        self.recovery_tool_name = recovery_tool_name or None
        self._registry = registry if registry is not None else breaker_registry
        #: ⚠️ 必须是**列表属性**，不能写成同名方法：
        #: langchain 的 create_agent 会做 `[t for m in middleware for t in getattr(m, "tools", [])]`
        #: （langchain/agents/factory.py:1005），方法对象不可迭代，会直接 TypeError 起不来。
        #: 这也正是 AgentMiddleware 基类把 `tools` 定义成 list 属性的原因。
        recovery_tool = self._build_recovery_tool()
        self.tools = [recovery_tool] if recovery_tool is not None else []

    @property
    def name(self) -> str:
        base = self.__class__.__name__
        return f"{base}[{self.tool_name}]" if self.tool_name else base

    # ---- 内部工具 ----
    def _tracked(self, called: str) -> bool:
        return self.tool_name is None or called == self.tool_name

    @staticmethod
    def _thread_id(request) -> str:
        """取会话标识（熔断按会话隔离；取不到就归到 default）。"""
        try:
            info = getattr(getattr(request, "runtime", None), "execution_info", None)
            tid = getattr(info, "thread_id", None)
            if tid:
                return str(tid)
        except Exception:
            pass
        try:
            cfg = getattr(request.runtime, "config", None) or {}
            tid = (cfg.get("configurable") or {}).get("thread_id")
            if tid:
                return str(tid)
        except Exception:
            pass
        return "default"

    def _gate(self, request, called: str) -> bool:
        """返回 True 表示放行本次调用；False 表示已熔断（应短路）。

        判定顺序很重要：
        1. 冷却中 → 挡（冷却窗口优先，`start_run` 不会解它）；
        2. 冷却已过且本次冷却还没探测过 → 放**一次**探测（半开，B 方案自愈）；
        3. 探测名额已用掉 → 挡（同一批并行里只放一个）；
        4. 计数已达阈值 → 立刻打开熔断并挡住（无需等 state 回来，批内也生效）。
        """
        thread_id = self._thread_id(request)
        b = self._registry.get(thread_id, called)
        now = time.monotonic()

        if b.is_cooling(now):
            return False
        if b.probe_allowed(now, self.cooldown_seconds):
            b.probe_used = True  # 半开：放一次探测
            logger.info(
                "[harness] 工具 %s 冷却结束，放行一次探测调用（上次探测在 %.0fs 前）",
                called, now - b.last_probe_at if b.last_probe_at else 0.0,
            )
            b.last_probe_at = now
            return True
        if b.cooling:
            # 冷却已过但还不该再探：本次冷却的探测名额用掉了，或距上次探测不足一个冷却期
            return False
        # 未熔断 → 放行。
        #
        # ⚠️ 这里曾经用 in_flight 做"占位预算"（把在飞的调用也算进失败额度），
        # 结果是**健康的并行批被误熔断**：模型一轮并行写 7 个文件时，前 5 个占满
        # 额度，第 6/7 个直接被挡，还会打出"连续失败 0 次 → 熔断"这种自相矛盾的日志。
        # 熔断的语义是"失败"，不是"批量大小"，所以准入只看真实失败计数。
        # in_flight 仅作诊断（观察并发度），不参与准入。
        b.in_flight += 1
        return True

    def _release(self, thread_id: str, called: str) -> None:
        """释放并发占位（仅诊断用）。"""
        b = self._registry.get(thread_id, called)
        if b.in_flight > 0:
            b.in_flight -= 1

    def _open(self, thread_id: str, called: str, now: float) -> None:
        b = self._registry.get(thread_id, called)
        b.cooling = True
        b.cooldown_until = now + self.cooldown_seconds
        b.probe_used = False
        logger.warning(
            "[harness] 工具 %s 连续失败 %d 次 → 熔断 %d 秒（期间调用被跳过）",
            called, b.consecutive_failures, self.cooldown_seconds,
        )

    def _on_failure(self, request, called: str) -> None:
        thread_id = self._thread_id(request)
        b = self._registry.get(thread_id, called)
        self._release(thread_id, called)
        b.consecutive_failures += 1
        logger.info(
            "[harness] 工具 %s 失败，连续失败=%d/%d",
            called, b.consecutive_failures, self.max_consecutive_failures,
        )
        if b.consecutive_failures >= self.max_consecutive_failures:
            self._open(thread_id, called, time.monotonic())

    def _on_success(self, request, called: str) -> None:
        thread_id = self._thread_id(request)
        b = self._registry.get(thread_id, called)
        self._release(thread_id, called)
        if b.consecutive_failures or b.cooling:
            logger.info("[harness] 工具 %s 恢复成功，熔断/计数已清零", called)
        self._registry.note_success(thread_id, called)

    # ---- 每轮开始：重置"连续失败预算"（跨轮不累计）----
    async def abefore_agent(self, state, runtime):
        """新一轮对话开始：把本会话所有工具的连续失败计数清零。

        效果：一轮里连续失败 5 次才熔断；下一轮工具又有新的机会（工具"过一会儿好了"
        就能被重新使用）。正在冷却中的窗口不受影响。
        """
        thread_id = self._thread_id_from_runtime(runtime)
        self._registry.start_run(thread_id)
        logger.debug("[harness] 新一轮开始，已重置会话 %s 的工具失败计数", thread_id)

    def _thread_id_from_runtime(self, runtime) -> str:
        """从 Runtime（非 ToolCallRequest）里取 thread_id。"""
        for getter in (
            lambda: (getattr(runtime, "config", None) or {}).get("configurable", {}).get("thread_id"),
            lambda: getattr(getattr(runtime, "execution_info", None), "thread_id", None),
        ):
            try:
                tid = getter()
                if tid:
                    return str(tid)
            except Exception:
                continue
        return "default"

    def _block_result(self, request, called: str):
        """短路：不执行工具，直接回一条"换方法"的结果。"""
        from langchain_core.messages import ToolMessage

        b = self._registry.get(self._thread_id(request), called)
        return ToolMessage(
            content=_build_switch_message(
                called,
                max(self.max_consecutive_failures, b.consecutive_failures),
                self.cooldown_seconds,
            ),
            tool_call_id=request.tool_call.get("id", ""),
            name=called,
            status="success",  # 主动跳过，不是错误
        )

    def _breaker_snapshot(self, request, called: str) -> dict:
        """当前熔断快照（诊断用，挂在 ToolMessage.additional_kwargs 上，不写 state）。

        为什么不写 state：同一批并行工具调用会在**同一个 superstep** 里各自返回
        `Command(update={"tool_failure_count": ...})`，而 LangGraph 规定"一个 step 内
        同一个 key 只能更新一次"（除非用 Annotated reducer）→ 直接抛
        `InvalidUpdateError: Can receive only one value per step`。
        熔断的权威状态本来就在进程内的 registry 里，所以这里不需要动 state。
        """
        b = self._registry.get(self._thread_id(request), called)
        return {
            "tool": called,
            "consecutive_failures": b.consecutive_failures,
            "cooling": b.cooling,
            "max_consecutive_failures": self.max_consecutive_failures,
        }

    # ---- 工具执行拦截 ----
    async def awrap_tool_call(self, request, handler):
        called = str(request.tool_call.get("name") or "")
        if not self._tracked(called):
            return await handler(request)

        if not self._gate(request, called):
            return self._block_result(request, called)

        try:
            result = await handler(request)
        except Exception:
            # 异常也算失败；记完原样抛出（保留已有错误处理链路）
            self._on_failure(request, called)
            raise

        # 只记在进程内的 registry 里，**不写 state**：并行批里多个调用各自返回
        # Command(update=同一个 key) 会触发 LangGraph 的 InvalidUpdateError。
        if _is_tool_failure(result):
            self._on_failure(request, called)
        else:
            self._on_success(request, called)
        return result

    def wrap_tool_call(self, request, handler):
        """同步版（deepagents 走异步链路；语义与异步版保持一致）。"""
        called = str(request.tool_call.get("name") or "")
        if not self._tracked(called):
            return handler(request)

        if not self._gate(request, called):
            return self._block_result(request, called)

        try:
            result = handler(request)
        except Exception:
            self._on_failure(request, called)
            raise

        if _is_tool_failure(result):
            self._on_failure(request, called)
        else:
            self._on_success(request, called)
        return result

    # ---- 手动恢复工具（D 方案）----
    def _build_recovery_tool(self):
        """构造"手动恢复"工具：清空**本会话**所有工具的熔断，让它们可以立即重试。

        为什么要按会话（而不是全局清空）：多个会话共用同一个进程内的熔断表，
        用户在 A 会话里说"再试一次"，不该顺手把 B 会话的熔断也解开。
        """
        if not self.recovery_tool_name:
            return None
        from langchain_core.tools import tool
        from langgraph.prebuilt.tool_node import ToolRuntime

        registry = self._registry
        recovery_name = self.recovery_tool_name

        @tool(recovery_name)
        def reset_tool_breakers(runtime: ToolRuntime, reason: str = "") -> str:
            """重置**本会话**所有工具的熔断状态，让之前失败/被冷却的工具可以立即重试。

            什么时候用：用户明确要求"再试一次某某工具/再搜一下"，或你判断外部服务
            可能已恢复时。调用本工具后再重试目标工具即可。

            Args:
                reason: 简述为什么要重置（例如"用户要求重试联网搜索"）。
            """
            thread_id = ""
            try:
                thread_id = str(getattr(runtime.execution_info, "thread_id", "") or "")
            except Exception:
                thread_id = ""
            cleared = registry.reset_all(thread_id or None)
            logger.info(
                "[harness] 手动重置工具熔断: session=%s 清空 %d 条（reason=%s）",
                thread_id or "(全部)", cleared, reason or "-",
            )
            return f"已重置 {cleared} 个工具的熔断状态，现在可以重新尝试之前失败的工具了。"

        return reset_tool_breakers


def build_failure_limit_middlewares() -> tuple[list[AgentMiddleware], list]:
    """按配置构建"连续失败熔断"中间件 + 配套的"手动恢复"工具。

    Returns:
        (中间件列表, 额外工具列表)。工具是 D 方案的入口：模型可主动调用它清空熔断，
        让用户说"再试一次搜索"时能立刻恢复（名字由 FAILURE_LIMIT_RESET_TOOL 决定，
        留空则不提供）。
    """
    from app.core.config import settings

    threshold = int(getattr(settings, "failure_limit_threshold", 0) or 0)
    if threshold <= 0:
        return [], []  # 未启用
    tool_name = (getattr(settings, "failure_limit_tool", "") or "").strip() or None
    cooldown = int(getattr(settings, "failure_limit_cooldown_seconds", 60) or 0)
    recovery = (getattr(settings, "failure_limit_reset_tool", "") or "").strip() or None
    mw = ConsecutiveToolFailureMiddleware(
        tool_name=tool_name,
        max_consecutive_failures=threshold,
        cooldown_seconds=cooldown,
        recovery_tool_name=recovery,
    )
    logger.info(
        "[harness] 工具熔断已启用: tool=%s 阈值=%d 冷却=%ds 手动恢复工具=%s",
        tool_name or "(所有工具各自)", threshold, cooldown, recovery or "(未提供)",
    )
    return [mw], list(mw.tools)


def build_tool_call_limit_middlewares() -> list[AgentMiddleware]:
    """按配置构建 langchain 现成的 `ToolCallLimitMiddleware`（工具调用次数上限）。

    用途：某个工具被反复调用（含失败后重试）超过上限后**降级** —— 该工具被挡掉，
    模型会收到 `Tool call limit exceeded. Do not call '<tool>' again.` 的
    `ToolMessage`，其他工具与模型继续执行。

    语义（读源码确认，`langchain/agents/middleware/tool_call_limit.py`）：
    - 统计的是**调用次数**，不是失败次数：模型每请求调用一次就 +1
      （`after_model` 里按 `last_ai_message.tool_calls` 累加）；
    - `thread_limit` 按**会话**累计（随 checkpointer 存档，跨轮次持久）；
      `run_limit` 按**一次运行**（一轮对话）累计；
    - 超过上限的那一次**不会真的执行**，只回一条错误 ToolMessage；
    - `exit_behavior="continue"`（默认）= 降级；"error" = 抛
      `ToolCallLimitExceededError`；"end" = 立即结束本轮（有并行工具调用时会报错）。

    配置见 `app/core/config.py` 的 TOOL_CALL_* ；两个上限都为 0 时不挂中间件。
    """
    from langchain.agents.middleware.tool_call_limit import ToolCallLimitMiddleware

    from app.core.config import settings

    thread_limit = int(getattr(settings, "tool_call_thread_limit", 0) or 0) or None
    run_limit = int(getattr(settings, "tool_call_run_limit", 0) or 0) or None
    if thread_limit is None and run_limit is None:
        return []  # 未配置任何上限：不限制工具调用次数

    # 库要求 run_limit <= thread_limit
    if thread_limit is not None and run_limit is not None and run_limit > thread_limit:
        logger.warning(
            "[harness] TOOL_CALL_RUN_LIMIT(%s) > TOOL_CALL_THREAD_LIMIT(%s)，已按 thread 上限收敛",
            run_limit, thread_limit,
        )
        run_limit = thread_limit

    tool_name = (getattr(settings, "tool_call_limit_tool", "") or "").strip() or None
    exit_behavior = (getattr(settings, "tool_call_limit_exit_behavior", "continue") or "continue").strip()
    if exit_behavior not in ("continue", "error", "end"):
        logger.warning("[harness] 未知的 TOOL_CALL_LIMIT_EXIT_BEHAVIOR=%r，回退 continue", exit_behavior)
        exit_behavior = "continue"

    mw = ToolCallLimitMiddleware(
        tool_name=tool_name,
        thread_limit=thread_limit,
        run_limit=run_limit,
        exit_behavior=exit_behavior,
    )
    logger.info(
        "[harness] 工具调用上限已启用: tool=%s thread_limit=%s run_limit=%s exit=%s",
        tool_name or "(全部工具)", thread_limit, run_limit, exit_behavior,
    )
    return [mw]

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


# ============================================================================
# P20：工具调用意图标签（`_intent`）
#
# 拆成两个"半场"，都在同一个中间件里，因为它们共享同一个字段契约：
#
#   1. **送模型之前**（`wrap_model_call`）：把 `_intent` 加进**发给模型的** JSON
#      Schema 的 properties 且列进 `required`。这是"让模型真的吐出来"的
#      **唯一可靠手段** —— 靠提示词请求字段名，模型会时灵时不灵；
#      靠 schema，字段名/类型/长度上限都是结构化的，不用从文本里正则抠。
#   2. **执行工具之前**（`wrap_tool_call`）：把 `_intent` **摘掉**再往下走。
#      它是给用户看的说明，不是工具参数；透传会让工具实现报
#      `unexpected keyword argument`，或把说明变成外部 API 的请求字段。
#
# ## 为什么改 schema 的方式是"复制一份"，而不是改工具本身
#
# `tool_call_schema` 同时被**模型绑定**（决定模型看到什么）和**参数校验**使用。
# 如果直接改注册在 ToolNode 里的那个工具对象，`_intent` 会变成工具的必填参数 ——
# 一旦模型忘了给，工具就**校验失败**，直接破坏"标签缺失/非法时 agent 仍能执行"
# 这条硬约束。所以这里只复制一份给模型看（`model_copy`），注册的那个对象
# 一个字节都不动：模型会尽量给，但我们从不让它成为执行的前置条件。
#
# ## 为什么 MCP 工具不注入（走第 3 层兜底）
#
# 适配器造出来的 MCP 工具，`args_schema` 是**远端给的原始 JSON Schema**，
# 我们只是在本地复述它。往里面塞字段等于替远端工具"改接口"，还会让
# "本地 schema"与"远端真实 schema"悄悄分叉。这类工具一律走目录兜底文案。
# ============================================================================


def _intent_property_schema() -> dict:
    """`_intent` 在 JSON Schema 里的样子（与数据契约一致）。"""
    from app.agent.intent import INTENT_FIELD_DESCRIPTION, INTENT_MAX_CHARS

    return {
        "type": "string",
        "maxLength": INTENT_MAX_CHARS,
        "description": INTENT_FIELD_DESCRIPTION,
    }


def augment_tool_schema(tool):
    """复制一个工具，并让它的 tool-call schema 多出必填的 `_intent`。

    Returns:
        新工具对象；**无法安全修改时返回 None**（调用方跳过，该工具走兜底文案）。
        返回 None 的两种情况：不是 `BaseTool`；schema 是原始 JSON Schema dict
        （MCP 远端工具，见上方说明）。
    """
    from langchain_core.tools import BaseTool

    from app.agent.intent import INTENT_FIELD

    if not isinstance(tool, BaseTool):
        return None
    schema = tool.tool_call_schema
    if isinstance(schema, dict):
        return None
    try:
        data = schema.model_json_schema()
        properties = dict(data.get("properties") or {})
        properties[INTENT_FIELD] = _intent_property_schema()
        required = list(data.get("required") or [])
        if INTENT_FIELD not in required:
            required.append(INTENT_FIELD)
        # 合并而不是重建：保住原 schema 的 $defs / additionalProperties / strict 等
        new_schema = {**data, "properties": properties, "required": required}
        return tool.model_copy(update={"args_schema": new_schema})
    except Exception as e:  # noqa: BLE001 展示层增强不该让建 agent 失败
        logger.warning("[harness] 工具 %s 的 schema 注入 _intent 失败: %s",
                       getattr(tool, "name", "?"), e)
        return None


class IntentMiddleware(AgentMiddleware):
    """意图标签：给模型看 schema（advertise=True）+ 从参数里剥离 `_intent`。

    Args:
        advertise: 是否把 `_intent` 注入送模型的 schema。
            主 Agent 用 True；子智能体用 **False** —— 子智能体的工具调用走
            `subagent_tool` 事件（本期的契约只覆盖 `tool_call`），不需要它们也
            产标签；但**剥离必须对子智能体同样生效**：模型完全可能自作主张多带
            一个 `_intent`（提示词/上下文里见过这个字段），那会直接变成
            "工具收到意外参数" 而报错。
    """

    def __init__(self, *, advertise: bool = True) -> None:
        super().__init__()
        self.advertise = bool(advertise)

    @property
    def name(self) -> str:
        return "harness-intent" if self.advertise else "harness-intent-strip"

    # ---- 半场一：送模型之前 ----

    def _augmented_tools(self, tools):
        if not self.advertise or not tools:
            return tools
        out = []
        for tool in tools:
            augmented = augment_tool_schema(tool)
            out.append(augmented if augmented is not None else tool)
        return out

    def wrap_model_call(self, request, handler):
        tools = self._augmented_tools(getattr(request, "tools", None) or [])
        return handler(request.override(tools=list(tools)))

    async def awrap_model_call(self, request, handler):
        tools = self._augmented_tools(getattr(request, "tools", None) or [])
        return await handler(request.override(tools=list(tools)))

    # ---- 半场二：执行工具之前 ----

    @staticmethod
    def _cleaned(request):
        """把 `_intent` 摘干净的请求（没有该字段时原样返回，避免无谓的对象复制）。"""
        from app.agent.intent import INTENT_FIELD, strip_intent

        args = request.tool_call.get("args")
        if not isinstance(args, dict) or INTENT_FIELD not in args:
            return request
        clean, _ = strip_intent(args)
        return request.override(tool_call={**request.tool_call, "args": clean})

    async def awrap_tool_call(self, request, handler):
        return await handler(self._cleaned(request))

    def wrap_tool_call(self, request, handler):
        return handler(self._cleaned(request))


def build_intent_middlewares() -> list[AgentMiddleware]:
    """构建主 Agent 的意图中间件（**总是返回一个**，只是开关决定要不要注入 schema）。

    为什么关掉时也留着：`_intent` 的**剥离**是硬不变量 —— 模型（或存档里的历史
    消息）完全可能带上这个字段，而工具收到意外参数会直接报错。要不要**请求**
    模型产出标签是配置项；要不要**清理**它是安全问题，不给开关。
    """
    from app.core.config import settings

    enabled = bool(getattr(settings, "intent_label_enabled", True))
    return [IntentMiddleware(advertise=enabled)]


def build_intent_strip_middleware() -> AgentMiddleware:
    """只剥离、不注入的版本（子智能体用；见 `IntentMiddleware.advertise`）。"""
    return IntentMiddleware(advertise=False)


# ============================================================================
# 自杀式命令护栏（P22）—— "按进程名杀 python" = 杀掉 harness 后端自己
#
# 事故（2026-10-09）：模型要"重启 9000 端口的 auth_app"，于是执行了
#     Stop-Process -Name python -Force -ErrorAction SilentlyContinue
# 这条命令把**本机所有 python 进程**都杀了 —— 包括正在跑这次对话的 uvicorn 后端。
# 后果：后端当场死亡 ⇒ 那一轮 run 永远发不出 `tool_result` ⇒ 工具流水永远停在
# `running` ⇒ 前端工具卡一直显示"进行中"，用户等到天亮也不会有任何反馈
# （实测 `logs/backend.log` 在那一刻直接停住，之后再无一行）。
#
# 为什么放在中间件而不是只写进提示词：提示词是"建议"，模型会忘；而这条命令的
# 代价是**整轮对话静默消失**，必须有硬拦截。
#
# ⚠️ 只拦"按**进程名**批量杀 python"这一族；`Stop-Process -Id <PID>`（按端口定位后
# 精确结束单个进程）是正确做法，**绝不能**误伤 —— 那正是我们推荐它用的方式。
# ============================================================================

_SELF_KILL_PATTERNS = (
    # PowerShell: Stop-Process -Name python [-Force]
    re.compile(r"stop-process[^\n|;&]*?-name\s+['\"]?pythonw?\b", re.IGNORECASE),
    # Windows cmd: taskkill /F /IM python.exe
    re.compile(r"taskkill[^\n|;&]*?/im\s+['\"]?pythonw?\.exe", re.IGNORECASE),
    # Unix: pkill python / killall python
    re.compile(r"\b(?:pkill|killall)\b[^\n|;&]*?\bpython", re.IGNORECASE),
    # WMIC 删除进程
    re.compile(r"wmic\s+process[^\n|;&]*?name\s*=\s*['\"]pythonw?\.exe['\"][^\n|;&]*?delete",
               re.IGNORECASE),
    # Get-Process -Name python | Stop-Process
    re.compile(r"get-process[^\n|;&]*?-name\s+['\"]?pythonw?\b[^\n]*?\|\s*stop-process",
               re.IGNORECASE),
)

_SELF_KILL_REFUSAL = (
    "Error: 已拒绝执行这条命令 —— 它会按**进程名**杀掉本机上所有 python 进程，"
    "而运行这次对话的后端服务本身就是其中一个 python 进程。"
    "上一次真的执行它时，后端当场被杀、这一轮永久没有结果、界面一直停在「进行中」。\n"
    "要重启服务，请**按端口精确定位**后只结束那一个进程：\n"
    "  netstat -ano | findstr :<端口>   （最后一列就是 PID）\n"
    "  Stop-Process -Id <PID> -Force\n"
    "不要用 `-Name python` / `/IM python.exe` 这类按进程名批量杀的方式。"
)


def _iter_strings(value):
    """递归取出参数里的所有字符串（命令可能藏在嵌套 dict/list 里）。"""
    if isinstance(value, str):
        yield value
    elif isinstance(value, dict):
        for v in value.values():
            yield from _iter_strings(v)
    elif isinstance(value, (list, tuple)):
        for v in value:
            yield from _iter_strings(v)


def find_self_kill_command(args) -> str | None:
    """参数里是否存在"按进程名杀 python"的命令片段；命中就返回那一小段（诊断用）。"""
    for text in _iter_strings(args):
        for pat in _SELF_KILL_PATTERNS:
            m = pat.search(text)
            if m:
                return m.group(0)
    return None


class SelfKillGuardMiddleware(AgentMiddleware):
    """拦截"按进程名杀 python"的自杀式命令，返回一条可解释的失败结果。

    返回 `ToolMessage(content=..., status="success")`：
    - 内容以 `Error:` 开头 ⇒ 会被 `classify_tool_result` 判为失败，前端显示"失败"；
    - `status` 留 `success` 是为了**不**把这条记进熔断器 —— 否则用户多点几次
      "重试"就莫名其妙把该工具熔断 60 秒。
    """

    @property
    def name(self) -> str:
        return "harness-self-kill-guard"

    def _check(self, request):
        from langchain_core.messages import ToolMessage

        hit = find_self_kill_command(request.tool_call.get("args") or {})
        if not hit:
            return None
        name = str(request.tool_call.get("name") or "tool")
        logger.warning("[harness] 已拦截自杀式命令 tool=%s hit=%r", name, hit)
        return ToolMessage(
            content=_SELF_KILL_REFUSAL,
            tool_call_id=request.tool_call.get("id", ""),
            name=name,
            status="success",
        )

    async def awrap_tool_call(self, request, handler):
        blocked = self._check(request)
        if blocked is not None:
            return blocked
        return await handler(request)

    def wrap_tool_call(self, request, handler):
        blocked = self._check(request)
        if blocked is not None:
            return blocked
        return handler(request)


def build_self_kill_guard_middleware() -> AgentMiddleware:
    """安全护栏：永远挂上（与权限模式无关 —— MCP 类工具同样在本机跑）。"""
    return SelfKillGuardMiddleware()
