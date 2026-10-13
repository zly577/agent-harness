"""测试：中间件框架（不触发真实模型调用）。"""
import sqlite3

import pytest

from app.agent.middleware import (
    AuditMiddleware,
    LoggingMiddleware,
    build_tool_call_limit_middlewares,
    _safe_messages,
)


def test_middleware_instantiable():
    """自研中间件都能实例化并具有预期 name。"""
    assert LoggingMiddleware().name == "harness-logging"
    assert AuditMiddleware().name == "harness-audit"


def test_safe_messages_handles_none():
    assert _safe_messages(None) == []
    assert _safe_messages({}) == []
    assert _safe_messages({"messages": ["a", "b"]}) == ["a", "b"]


def test_tool_call_limit_uses_library_middleware(monkeypatch):
    """工具调用次数限制用的是 langchain 现成的 ToolCallLimitMiddleware。

    注意：这一层的默认是**关闭**（TOOL_CALL_THREAD_LIMIT=0），因为"连续失败降级"
    才是默认启用的那条路。这里显式打开，避免测试依赖默认值。
    """
    from langchain.agents.middleware.tool_call_limit import ToolCallLimitMiddleware

    from app.core.config import settings

    monkeypatch.setattr(settings, "tool_call_thread_limit", 5, raising=False)
    monkeypatch.setattr(settings, "tool_call_limit_tool", "web_search", raising=False)
    mws = build_tool_call_limit_middlewares()
    assert mws, "打开 TOOL_CALL_THREAD_LIMIT 后应产出一个限制中间件"
    mw = mws[0]
    assert isinstance(mw, ToolCallLimitMiddleware)
    assert mw.tool_name == "web_search", mw.tool_name
    assert mw.thread_limit == 5, mw.thread_limit
    assert mw.exit_behavior == "continue", mw.exit_behavior


def test_tool_call_limit_disabled_by_default():
    """默认配置（TOOL_CALL_THREAD_LIMIT=0 且 RUN_LIMIT=0）不挂调用次数中间件。"""
    assert build_tool_call_limit_middlewares() == []


def test_tool_call_limit_can_be_disabled(monkeypatch):
    """两个上限都为 0 → 不挂中间件（完全放开）。"""
    from app.core.config import settings

    monkeypatch.setattr(settings, "tool_call_thread_limit", 0, raising=False)
    monkeypatch.setattr(settings, "tool_call_run_limit", 0, raising=False)
    assert build_tool_call_limit_middlewares() == []


def test_tool_call_limit_all_tools_when_tool_empty(monkeypatch):
    """tool_call_limit_tool 留空 → 限制所有工具合计。"""
    from app.core.config import settings

    monkeypatch.setattr(settings, "tool_call_thread_limit", 5, raising=False)
    monkeypatch.setattr(settings, "tool_call_limit_tool", "", raising=False)
    assert build_tool_call_limit_middlewares()[0].tool_name is None


def test_tool_call_limit_converges_run_to_thread(monkeypatch):
    """库要求 run_limit <= thread_limit；配错时自动收敛而不是抛异常。"""
    from app.core.config import settings

    monkeypatch.setattr(settings, "tool_call_thread_limit", 3, raising=False)
    monkeypatch.setattr(settings, "tool_call_run_limit", 10, raising=False)
    mw = build_tool_call_limit_middlewares()[0]
    assert mw.thread_limit == 3 and mw.run_limit == 3


# ===== 连续失败降级（ConsecutiveToolFailureMiddleware）=====


class _Req:
    """awrap_tool_call 的最小请求替身。"""

    def __init__(self, name: str, state: dict | None = None):
        self.tool_call = {"name": name, "id": f"id-{name}", "args": {}}
        self.state = state if state is not None else {}


def _msg(content: str, status: str = "success"):
    from langchain_core.messages import ToolMessage

    return ToolMessage(content=content, tool_call_id="id-x", status=status)


def test_is_tool_failure_criteria():
    """失败判定口径：status=error / 文本 Error: 开头 / dict 非空 error。"""
    from app.agent.middleware import _is_tool_failure

    assert _is_tool_failure(_msg("x", "error")) is True
    assert _is_tool_failure(_msg("Error: nope")) is True
    assert _is_tool_failure(_msg("all good")) is False
    assert _is_tool_failure({"error": "file_not_found"}) is True
    assert _is_tool_failure({"error": "", "path": "/a"}) is False
    assert _is_tool_failure({"path": "/a"}) is False


def test_failure_limit_blocks_after_five_consecutive_failures():
    """连续失败 5 次后，第 6 次起不再执行（短路）——不依赖模型行为。"""
    import asyncio

    from app.agent.middleware import ConsecutiveToolFailureMiddleware, ToolBreakerRegistry

    # 注入独立 registry：熔断表是进程内的，测试必须彼此隔离，否则互相污染
    reg = ToolBreakerRegistry()
    mw = ConsecutiveToolFailureMiddleware(
        tool_name="flaky", max_consecutive_failures=5,
        cooldown_seconds=3600, registry=reg,
    )
    state: dict = {}
    executed = 0
    blocked = 0

    async def handler(req):
        nonlocal executed
        executed += 1
        return _msg("Error: boom", "error")

    for _ in range(7):
        res = asyncio.run(mw.awrap_tool_call(_Req("flaky", state), handler))
        if "被跳过" in str(res.content):  # 短路文案：工具未执行
            blocked += 1
            assert "冷却期" in str(res.content)
            assert "reset_tool_breakers" in str(res.content)  # 提示可手动恢复
        else:  # 工具真的执行了
            assert executed <= 5

    assert executed == 5, f"应只执行 5 次，实际 {executed}"
    assert blocked == 2, f"后两次应被短路，实际 {blocked}"
    assert reg.get("default", "flaky").consecutive_failures == 5


def test_failure_count_resets_on_success():
    """成功一次即清零（这是与 ToolCallLimitMiddleware 的关键区别）。"""
    import asyncio

    from app.agent.middleware import ConsecutiveToolFailureMiddleware, ToolBreakerRegistry

    reg = ToolBreakerRegistry()
    mw = ConsecutiveToolFailureMiddleware(
        tool_name="flaky", max_consecutive_failures=5, registry=reg
    )
    state: dict = {}

    async def fail_handler(req):
        return _msg("Error: boom", "error")

    async def ok_handler(req):
        return _msg("fine")

    for _ in range(3):  # 先攒 3 次失败
        asyncio.run(mw.awrap_tool_call(_Req("flaky", state), fail_handler))
    assert reg.get("default", "flaky").consecutive_failures == 3

    res = asyncio.run(mw.awrap_tool_call(_Req("flaky", state), ok_handler))
    assert res.content == "fine"                      # 正常透传工具结果
    assert reg.get("default", "flaky").consecutive_failures == 0   # 计数清零


def test_failure_limit_ignores_other_tools():
    """只统计指定工具时，其他工具不受影响（不被短路）。"""
    import asyncio

    from app.agent.middleware import ConsecutiveToolFailureMiddleware, ToolBreakerRegistry

    reg = ToolBreakerRegistry()
    mw = ConsecutiveToolFailureMiddleware(
        tool_name="flaky", max_consecutive_failures=5, registry=reg
    )
    reg.get("default", "flaky").consecutive_failures = 5   # flaky 已到阈值

    async def ok_handler(req):
        return _msg("ok")

    res = asyncio.run(mw.awrap_tool_call(_Req("other"), ok_handler))
    # 直接执行 → 返回 handler 的结果，而不是短路
    assert res.content == "ok"


def test_failure_limit_builder_respects_config(monkeypatch):
    """工厂函数：threshold=0 关闭；>0 时带上阈值/冷却，并产出"手动恢复"工具。"""
    from app.agent.middleware import build_failure_limit_middlewares
    from app.core.config import settings

    monkeypatch.setattr(settings, "failure_limit_threshold", 0, raising=False)
    mws, tools = build_failure_limit_middlewares()
    assert mws == [] and tools == []

    monkeypatch.setattr(settings, "failure_limit_threshold", 5, raising=False)
    monkeypatch.setattr(settings, "failure_limit_cooldown_seconds", 30, raising=False)
    monkeypatch.setattr(settings, "failure_limit_tool", "", raising=False)
    monkeypatch.setattr(settings, "failure_limit_reset_tool", "reset_tool_breakers", raising=False)
    mws, tools = build_failure_limit_middlewares()
    assert mws[0].tool_name is None, "留空 = 所有工具各自独立熔断"
    assert mws[0].max_consecutive_failures == 5
    assert mws[0].cooldown_seconds == 30
    assert [t.name for t in tools] == ["reset_tool_breakers"]


# ===== 熔断状态机：B（冷却后探测自愈）+ D（手动恢复）=====


class _FakeInfo:
    def __init__(self, tid):
        self.thread_id = tid


class _FakeRuntime:
    def __init__(self, tid):
        self.execution_info = _FakeInfo(tid)


def mk_breaker_req(name: str, thread: str = "T1"):
    """构造一个带 thread_id 的请求替身。"""
    r = _Req(name)
    r.runtime = _FakeRuntime(thread)
    return r


async def run_tool(mw, req, *, ok=False, raise_exc=False):
    """跑一次调用；返回 'executed' | 'blocked'。

    中间件不再写 state（否则并行批会触发 InvalidUpdateError），所以用"短路专属文案"
    来区分：被短路的结果里含"被跳过"。
    """
    async def handler(_req):
        if raise_exc:
            raise RuntimeError("boom")
        return _msg("fine" if ok else "Error: down")

    try:
        res = await mw.awrap_tool_call(req, handler)
    except RuntimeError:
        return "executed"  # 异常被原样抛出（但已计入失败）
    return "blocked" if "被跳过" in str(getattr(res, "content", "")) else "executed"


def test_breaker_opens_after_five_and_blocks():
    """连续失败 5 次后熔断：第 6 次起短路（不执行）。"""
    import asyncio

    from app.agent.middleware import ConsecutiveToolFailureMiddleware, ToolBreakerRegistry

    reg = ToolBreakerRegistry()
    mw = ConsecutiveToolFailureMiddleware(max_consecutive_failures=5, cooldown_seconds=60, registry=reg)
    req = mk_breaker_req("search")
    seq = [asyncio.run(run_tool(mw, req)) for _ in range(7)]
    assert seq == ["executed"] * 5 + ["blocked", "blocked"]
    assert "search" in reg.cooling_tools("T1")


def test_breaker_half_open_recovers_when_tool_is_healthy_again():
    """B：冷却过后放一次探测，探测成功即完全恢复（工具"过一会儿好了"能再用）。"""
    import asyncio

    from app.agent.middleware import ConsecutiveToolFailureMiddleware, ToolBreakerRegistry

    reg = ToolBreakerRegistry()
    mw = ConsecutiveToolFailureMiddleware(max_consecutive_failures=5, cooldown_seconds=0, registry=reg)
    req = mk_breaker_req("search")
    for _ in range(5):
        asyncio.run(run_tool(mw, req))
    assert asyncio.run(run_tool(mw, req, ok=True)) == "executed"   # 探测成功
    assert asyncio.run(run_tool(mw, req, ok=True)) == "executed"   # 恢复正常
    assert reg.cooling_tools("T1") == {}


def test_breaker_probe_backoff_prevents_hammering():
    """探测失败后不能立刻再探（最小间隔），否则 COOLDOWN=0 时会无限重试。"""
    import asyncio

    from app.agent.middleware import ConsecutiveToolFailureMiddleware, ToolBreakerRegistry

    reg = ToolBreakerRegistry()
    mw = ConsecutiveToolFailureMiddleware(max_consecutive_failures=5, cooldown_seconds=0, registry=reg)
    req = mk_breaker_req("search")
    for _ in range(5):
        asyncio.run(run_tool(mw, req))
    assert asyncio.run(run_tool(mw, req)) == "executed"    # 探测（失败）
    assert asyncio.run(run_tool(mw, req)) == "blocked"     # 间隔未到 → 挡住


def test_parallel_batch_is_not_mistaken_for_failures():
    """回归（重要）：健康的并行批**不能**被误熔断。

    曾经用 in_flight 当"失败额度"，导致模型一轮并行写 7 个文件时前 5 个占满额度、
    第 6/7 个被挡，还打出"连续失败 0 次 → 熔断"的自相矛盾日志。
    熔断语义是"失败"，不是"批量大小"。
    """
    import asyncio

    from app.agent.middleware import ConsecutiveToolFailureMiddleware, ToolBreakerRegistry

    reg = ToolBreakerRegistry()
    mw = ConsecutiveToolFailureMiddleware(max_consecutive_failures=5, cooldown_seconds=60, registry=reg)
    executed = {"n": 0}

    async def one(_i):
        async def handler(_req):
            executed["n"] += 1
            await asyncio.sleep(0.01)
            return _msg("fine")            # 全部成功

        res = await mw.awrap_tool_call(mk_breaker_req("search"), handler)
        return "blocked" if "被跳过" in str(getattr(res, "content", "")) else "executed"

    async def batch():
        return await asyncio.gather(*[one(i) for i in range(7)])

    results = asyncio.run(batch())
    assert executed["n"] == 7, f"健康的并行批应全部执行，实际 {executed}"
    assert results.count("blocked") == 0, results
    assert reg.get("T1", "search").consecutive_failures == 0
    assert reg.cooling_tools("T1") == {}, "成功的批不应触发熔断"


def test_parallel_batch_of_failures_trips_after_batch():
    """并行批里全是失败时：批内都执行（并发在飞时彼此看不到），批后计数达阈值即熔断。"""
    import asyncio

    from app.agent.middleware import ConsecutiveToolFailureMiddleware, ToolBreakerRegistry

    reg = ToolBreakerRegistry()
    mw = ConsecutiveToolFailureMiddleware(max_consecutive_failures=5, cooldown_seconds=60, registry=reg)

    async def one(_i):
        async def handler(_req):
            await asyncio.sleep(0.01)
            return _msg("Error: down")

        res = await mw.awrap_tool_call(mk_breaker_req("search"), handler)
        return "blocked" if "被跳过" in str(getattr(res, "content", "")) else "executed"

    async def batch():
        return await asyncio.gather(*[one(i) for i in range(7)])

    results = asyncio.run(batch())
    # 批内并发时都在飞，互相看不到 → 会都执行；但失败都被记账
    assert reg.get("T1", "search").consecutive_failures >= 5, results
    # 已经熔断：下一批必被挡
    assert asyncio.run(run_tool(mw, mk_breaker_req("search"))) == "blocked"


def test_breaker_is_per_tool_and_per_session():
    """每个工具独立熔断；熔断按会话隔离。"""
    import asyncio

    from app.agent.middleware import ConsecutiveToolFailureMiddleware, ToolBreakerRegistry

    reg = ToolBreakerRegistry()
    mw = ConsecutiveToolFailureMiddleware(max_consecutive_failures=5, cooldown_seconds=60, registry=reg)
    a, b = mk_breaker_req("tool_a"), mk_breaker_req("tool_b")
    for _ in range(5):
        asyncio.run(run_tool(mw, a))
    assert asyncio.run(run_tool(mw, a)) == "blocked"
    assert asyncio.run(run_tool(mw, b, ok=True)) == "executed"     # 别的工具不受影响
    assert asyncio.run(run_tool(mw, mk_breaker_req("tool_a", thread="OTHER"), ok=True)) == "executed"


def test_breaker_start_run_resets_failure_budget():
    """新一轮对话重置"连续失败预算"（跨轮不累计），但冷却窗口不受影响。"""
    import asyncio

    from app.agent.middleware import ConsecutiveToolFailureMiddleware, ToolBreakerRegistry

    reg = ToolBreakerRegistry()
    mw = ConsecutiveToolFailureMiddleware(max_consecutive_failures=5, cooldown_seconds=3600, registry=reg)
    req = mk_breaker_req("search")
    for _ in range(4):
        asyncio.run(run_tool(mw, req))
    reg.start_run("T1")                       # 新一轮
    assert reg.get("T1", "search").consecutive_failures == 0
    for _ in range(5):
        asyncio.run(run_tool(mw, req))
    assert asyncio.run(run_tool(mw, req)) == "blocked"           # 又攒满 5 次才熔断
    reg.start_run("T1")                                        # 冷却窗口内重置预算
    assert asyncio.run(run_tool(mw, req)) == "blocked"            # 冷却仍生效，不会被重置解开


def test_manual_reset_tool_clears_breakers():
    """D：reset_tool_breakers 工具能清空本会话熔断，让工具立刻可重试。"""
    import asyncio

    from app.agent.middleware import ConsecutiveToolFailureMiddleware, ToolBreakerRegistry

    reg = ToolBreakerRegistry()
    mw = ConsecutiveToolFailureMiddleware(
        max_consecutive_failures=5, cooldown_seconds=3600,
        recovery_tool_name="reset_tool_breakers", registry=reg,
    )
    req = mk_breaker_req("search")
    for _ in range(5):
        asyncio.run(run_tool(mw, req))
    assert asyncio.run(run_tool(mw, req)) == "blocked"

    tools = {t.name: t for t in mw.tools}
    assert "reset_tool_breakers" in tools
    out = tools["reset_tool_breakers"].func(runtime=_FakeRuntime("T1"), reason="用户要求重试")
    assert "已重置" in out
    assert asyncio.run(run_tool(mw, req, ok=True)) == "executed"


def test_exception_counts_as_failure():
    """抛异常同样计入连续失败。"""
    import asyncio

    from app.agent.middleware import ConsecutiveToolFailureMiddleware, ToolBreakerRegistry

    reg = ToolBreakerRegistry()
    mw = ConsecutiveToolFailureMiddleware(max_consecutive_failures=5, cooldown_seconds=60, registry=reg)
    req = mk_breaker_req("boom_tool")
    for _ in range(5):
        asyncio.run(run_tool(mw, req, raise_exc=True))
    assert asyncio.run(run_tool(mw, req)) == "blocked"


def test_middleware_tools_is_a_list_attribute():
    """回归：`tools` 必须是【列表属性】，不能写成同名方法。

    langchain 的 create_agent 会执行
    `[t for m in middleware for t in getattr(m, "tools", [])]`
    （langchain/agents/factory.py:1005）。方法对象不可迭代，写错会让后端起不来：
        TypeError: 'method' object is not iterable
    """
    from app.agent.middleware import ConsecutiveToolFailureMiddleware

    mw = ConsecutiveToolFailureMiddleware(max_consecutive_failures=5)
    assert isinstance(mw.tools, list), f"tools 应为 list，实际 {type(mw.tools)}"
    assert [t.name for t in mw.tools] == ["reset_tool_breakers"]

    # 关掉手动恢复工具时，也必须是（空）列表而不是 None/方法
    mw2 = ConsecutiveToolFailureMiddleware(max_consecutive_failures=5, recovery_tool_name=None)
    assert mw2.tools == []


def test_factory_style_tool_collection_works():
    """回归：复刻 factory.py:1005 的收集方式，不能抛 TypeError。"""
    from langchain.agents.middleware.types import AgentMiddleware

    from app.agent.middleware import ConsecutiveToolFailureMiddleware

    class _DummyMiddleware(AgentMiddleware):
        pass

    mw = ConsecutiveToolFailureMiddleware(max_consecutive_failures=5, cooldown_seconds=60)
    collected = [t for m in [mw, _DummyMiddleware()] for t in getattr(m, "tools", [])]
    assert [t.name for t in collected] == ["reset_tool_breakers"]


def test_parallel_tools_do_not_write_shared_state_key():
    """回归：中间件不得在工具调用里写 graph state。

    同一批并行工具调用会在同一个 superstep 内各自返回
    `Command(update={"tool_failure_count": ...})` → LangGraph 抛
    `InvalidUpdateError: At key 'tool_failure_count': Can receive only one value per step`。
    熔断状态在进程内 registry 里，返回值必须原样透传 handler 的结果。
    """
    import asyncio

    from langchain_core.messages import ToolMessage

    from app.agent.middleware import ConsecutiveToolFailureMiddleware, ToolBreakerRegistry

    reg = ToolBreakerRegistry()
    mw = ConsecutiveToolFailureMiddleware(max_consecutive_failures=5, registry=reg)

    async def handler(_req):
        return ToolMessage(content="ok", tool_call_id="id-search")

    async def one():
        return await mw.awrap_tool_call(mk_breaker_req("search"), handler)

    async def gather_all():
        return await asyncio.gather(*[one() for _ in range(3)])

    results = asyncio.run(gather_all())
    for res in results:
        assert isinstance(res, ToolMessage), type(res)
        assert not hasattr(res, "update"), "不应返回 Command（会引发 InvalidUpdateError）"


def test_audit_log_model_columns():
    """audit_logs 模型字段正确。"""
    from app.db.models import AuditLog
    cols = [c.name for c in AuditLog.__table__.columns]
    assert {"id", "thread_id", "run_id", "action", "detail", "created_at"} <= set(cols)


def test_audit_write_to_mysql():
    """_write_audit 能把一条记录写入 MySQL（真实审计入库）。"""
    import asyncio
    from sqlalchemy import select

    async def _run():
        from app.agent.middleware import _write_audit
        await _write_audit(
            thread_id="atest-thread",
            run_id="atest-run",
            action="test_event",
            detail={"k": "v"},
        )
    asyncio.run(_run())

    from app.db.session import SessionLocal
    from app.db.models import AuditLog
    db = SessionLocal()
    try:
        row = db.execute(
            select(AuditLog).where(AuditLog.thread_id == "atest-thread").limit(1)
        ).scalar_one_or_none()
        assert row is not None
        assert row.action == "test_event"
        assert '{"k": "v"}' in (row.detail or "")
        # 清理测试记录，避免污染
        db.delete(row)
        db.commit()
    finally:
        db.close()
