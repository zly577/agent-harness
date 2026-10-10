"""审批模块单元测试（不依赖真实模型/网络）。"""
import asyncio

import pytest

from app.agent.approvals import (
    build_decision,
    build_resume_map,
    read_pending_approval,
)


def test_build_decision_approve():
    actions = [{"type": "tool_call"}, {"type": "tool_call"}]
    dec = build_decision(actions, approve=True)
    assert dec == [{"type": "approve"}, {"type": "approve"}]


def test_build_decision_reject():
    actions = [{"type": "tool_call"}]
    dec = build_decision(actions, approve=False)
    assert dec == [{"type": "reject"}]


def test_build_decision_no_actions_defaults_one():
    # 无 actions 时默认构造一个决策（用于单待审批项）
    dec = build_decision(None, approve=True)
    assert dec == [{"type": "approve"}]


# ===== 多中断恢复（回归：点同意/拒绝没反应）=====
#
# 现场：主智能体在同一 superstep 里并行委派 5 个子智能体，5 个子智能体各自 write_file
# → checkpoint 里同时挂 5 个中断。旧实现只读 interrupts[0] 并用扁平的
# Command(resume={"decisions":[...]})，langgraph 抛
# "When there are multiple pending interrupts, you must specify the interrupt id
#  when resuming."，前端把弹窗放回来于是看起来"点什么都无效"。


def test_build_resume_map_single_interrupt():
    groups = [{"id": "a" * 32, "actions": [{"tool": "write_file"}]}]
    resume = build_resume_map(groups, approve=True)
    assert resume == {"a" * 32: {"decisions": [{"type": "approve"}]}}


def test_build_resume_map_multiple_interrupts():
    """并行委派留下的多个中断：每个 id 都要有自己的决策，缺一个就还会卡住。"""
    ids = [c * 32 for c in "abcde"]
    groups = [{"id": i, "actions": [{"tool": "write_file"}]} for i in ids]
    resume = build_resume_map(groups, approve=True)
    assert set(resume) == set(ids)
    assert all(v == {"decisions": [{"type": "approve"}]} for v in resume.values())


def test_build_resume_map_reject_all():
    ids = [c * 32 for c in "ab"]
    groups = [{"id": i, "actions": [{"tool": "write_file"}]} for i in ids]
    resume = build_resume_map(groups, approve=False)
    assert all(v == {"decisions": [{"type": "reject"}]} for v in resume.values())


def test_build_resume_map_decisions_match_action_count():
    """一个中断下有 N 个 action，就要给 N 个决策（与 tool_call 一一对应）。"""
    groups = [{"id": "f" * 32, "actions": [{"tool": "a"}, {"tool": "b"}, {"tool": "c"}]}]
    resume = build_resume_map(groups, approve=True)
    assert len(resume["f" * 32]["decisions"]) == 3


def test_build_resume_map_skips_groups_without_id():
    """拿不到 id 的中断无法定向恢复 —— 跳过，但不影响其它中断。"""
    groups = [{"id": "", "actions": [{"tool": "x"}]}, {"id": "b" * 32, "actions": [{"tool": "y"}]}]
    resume = build_resume_map(groups, approve=True)
    assert set(resume) == {"b" * 32}


def test_build_resume_map_empty():
    assert build_resume_map(None, approve=True) == {}
    assert build_resume_map([], approve=True) == {}


class _FakeState:
    def __init__(self, interrupts, next_tasks=("HumanInTheLoopMiddleware.after_model",)):
        self.interrupts = interrupts
        #: "还没跑完的节点"。真实 langgraph 在挂起时非空、跑到底时为空 ——
        #: read_pending_approval 用它排除"已经处理过但还留在快照里"的陈旧中断。
        self.next = next_tasks


class _FakeInterrupt:
    def __init__(self, iid, value):
        self.id = iid
        self.value = value


class _FakeAgent:
    """只实现 aget_state —— read_pending_approval 用到的唯一接口。"""

    def __init__(self, interrupts, next_tasks=("HumanInTheLoopMiddleware.after_model",)):
        self._interrupts = interrupts
        self._next = next_tasks

    async def aget_state(self, config):
        return _FakeState(self._interrupts, self._next)


def _hitl_request(file_path: str) -> dict:
    return {
        "action_requests": [{
            "name": "write_file",
            "args": {"file_path": file_path, "content": "x" * 400},
            "description": "写入工作区文件",
        }],
        "review_configs": [],
    }


def test_read_pending_approval_no_interrupt():
    agent = _FakeAgent([])
    info = asyncio.run(read_pending_approval(agent, {"configurable": {"thread_id": "t"}}))
    assert info == {"pending": False}


def test_read_pending_approval_returns_all_interrupts():
    """回归核心：5 个中断必须全部读出来，不能只读第一个。"""
    ids = [c * 32 for c in "abcde"]
    agent = _FakeAgent([
        _FakeInterrupt(i, _hitl_request(f"/workspace/part{n}.md"))
        for n, i in enumerate(ids, start=1)
    ])
    info = asyncio.run(read_pending_approval(agent, {"configurable": {"thread_id": "t"}}))

    assert info["pending"] is True
    # 展平后 5 个 action，弹窗能逐条展示
    assert len(info["actions"]) == 5
    assert [a["tool"] for a in info["actions"]] == ["write_file"] * 5
    assert [a["args"]["file_path"] for a in info["actions"]] == [
        f"/workspace/part{n}.md" for n in range(1, 6)
    ]
    # 中断 id 齐全，可用于组装恢复映射
    assert [g["id"] for g in info["interrupts"]] == ids
    # 端到端：读出来的东西直接就能组出 5 个键的恢复映射
    resume = build_resume_map(info["interrupts"], approve=True)
    assert set(resume) == set(ids)


def test_read_pending_approval_pending_even_if_value_unrecognized():
    """中断 value 形状不认识时也必须 pending=True —— 否则用户永远处理不掉它。"""
    agent = _FakeAgent([_FakeInterrupt("c" * 32, "看不懂的东西")])
    info = asyncio.run(read_pending_approval(agent, {"configurable": {"thread_id": "t"}}))
    assert info["pending"] is True
    assert len(info["interrupts"]) == 1


# ===== 陈旧中断必须被排除（自动执行后仍弹窗 / 点同意说已审批过）=====


def test_read_pending_approval_ignores_stale_interrupts_when_graph_finished():
    """图已经跑到底（next 为空）时，快照里残留的中断不算"待审批"。

    现场：用 resume map 恢复后 langgraph 不写 per-task RESUME 记录，已处理完的中断
    仍留在 `Snapshot.interrupts` 里。旧判据（只看 interrupts）会让弹窗再次列出
    **已经写完的文件**，用户点同意 → 409「无待审批项」→ 提示"已审批过"。
    """
    agent = _FakeAgent(
        [_FakeInterrupt("a" * 32, _hitl_request("/workspace/part1.md"))],
        next_tasks=(),          # 图跑完了
    )
    info = asyncio.run(read_pending_approval(agent, {"configurable": {"thread_id": "t"}}))
    assert info == {"pending": False}


def test_read_pending_approval_still_pending_when_graph_suspended():
    """反向保护：图确实停在审批上（next 非空）时必须照常报 pending。"""
    agent = _FakeAgent([_FakeInterrupt("b" * 32, _hitl_request("/workspace/x.md"))])
    info = asyncio.run(read_pending_approval(agent, {"configurable": {"thread_id": "t"}}))
    assert info["pending"] is True
    assert [g["id"] for g in info["interrupts"]] == ["b" * 32]


def test_read_pending_approval_registers_and_forgets_session():
    """读过 pending 的会话要进登记表（关闭人工介入时靠它找回收尾对象）。"""
    from app.agent import approvals

    sid = "reg-test-session"
    approvals.forget_pending(sid)
    assert sid not in approvals.known_pending_sessions()

    agent = _FakeAgent([_FakeInterrupt("d" * 32, _hitl_request("/workspace/y.md"))])
    asyncio.run(read_pending_approval(agent, {"configurable": {"thread_id": sid}}))
    assert sid in approvals.known_pending_sessions()

    approvals.forget_pending(sid)
    assert sid not in approvals.known_pending_sessions()


# ===== 图结构必须与开关无关（修复的核心）=====


def test_hitl_interrupt_on_keeps_middleware_when_disabled(monkeypatch, tmp_path):
    """关闭人工介入要返回**非空**占位表 —— 空表会让 deepagents 不挂 HITL 中间件，
    图少一个节点，已挂起的中断就会"隐身"（点同意 → 无待审批项），
    重新打开时又"复活"（弹窗再现）。

      补充：**"没有审批规则"也必须给占位表**。以前总有一条 `/workspace/**` 兜着，
    所以这条路径走不到；现在审批范围收窄到"本机挂载点"，没挂本机就是空规则
    （见本测试前半段），图结构依然必须保持一致。
    """
    from app.agent import permissions
    from app.core.config import settings

    sid = "shape-test-session"
    # 环境 A：没有本机挂载点 ⇒ 规则天然为空，也必须挂中间件
    monkeypatch.setattr(settings, "harness_machine_access", False)
    monkeypatch.setattr(settings, "harness_machine_mounts", "")
    permissions.set_hitl(True, sid)
    try:
        assert permissions.agent_permissions(sid) == []
        assert permissions.hitl_interrupt_on(sid) == {
            permissions.DISABLED_INTERRUPT_KEY: False
        }, "空规则时同样要给占位键，否则图少一个节点"
    finally:
        permissions._session_hitl.pop(sid, None)

    # 环境 B：挂了本机 ⇒ 开 = 有 interrupt 权限且审批表交给 permissions 生成；
    #          关 = 空规则 + 占位键
    host = tmp_path / "host"
    host.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(settings, "harness_machine_access", True)
    monkeypatch.setattr(settings, "harness_machine_mounts", f"tmp={host.as_posix()}")

    permissions.set_hitl(False, sid)
    try:
        off = permissions.hitl_interrupt_on(sid)
        assert off, "关闭时必须给占位键，否则中间件不会被装配"
        # 占位键不能是真实工具名（否则会真的拦下工具）
        assert permissions.DISABLED_INTERRUPT_KEY in off
        assert off[permissions.DISABLED_INTERRUPT_KEY] is False
        assert permissions.agent_permissions(sid) == []

        permissions.set_hitl(True, sid)
        assert permissions.hitl_interrupt_on(sid) is None, "开启时审批表交给 permissions 生成"
        assert permissions.agent_permissions(sid), "开启时应生成 interrupt 权限"
    finally:
        permissions.set_hitl(True, sid)
        permissions._session_hitl.pop(sid, None)


# ===== 最终回复的兜底（工具都跑完了却没有回复）=====


def _ai(text, tool_calls=None):
    from langchain_core.messages import AIMessage

    return AIMessage(content=text, tool_calls=tool_calls or [])


def test_last_assistant_text_skips_tool_call_only_message():
    """最后一条常常是"只有 tool_calls、content 为空"的 AIMessage —— 取其 content
    会得到空字符串，这正是"什么都没有回复"的一个来源。要往前找有文本的那条。"""
    from app.agent.runtime import _last_assistant_text

    msgs = [
        _ai("先看目录。"),
        _ai("", tool_calls=[{"id": "1", "name": "write_file", "args": {}}]),
    ]
    assert _last_assistant_text(msgs) == "先看目录。"


def test_last_assistant_text_prefers_latest_non_empty():
    from app.agent.runtime import _last_assistant_text

    assert _last_assistant_text([_ai("旧的"), _ai("新的")]) == "新的"
    assert _last_assistant_text([]) == ""


def test_empty_reply_fallback_is_never_empty():
    """兜底回复必须永远有内容：空字符串在前端等于"什么都没发生"。"""
    from app.agent.runtime import _empty_reply_fallback

    text = _empty_reply_fallback("no-such-session")
    assert text.strip()
    assert "没有产出文字" in text


def test_sse_event_forwards_approval_text():
    """approval 事件要把 text 带给前端，否则气泡里一片空白。"""
    from app.agent.runner import to_sse_event

    evt = to_sse_event("approval", {"pending": True, "actions": [{"tool": "write_file"}],
                                    "text": "我准备写入 /workspace/a.md"})
    assert evt["type"] == "approval"
    assert evt["pending"] is True
    assert evt["text"] == "我准备写入 /workspace/a.md"
    # 缺字段时给空串，前端不必判 undefined
    assert to_sse_event("approval", {"pending": False})["text"] == ""


def test_clear_pending_interrupt_sweeps_registry(monkeypatch):
    """全局关闭人工介入时必须把**所有**登记过的会话都收尾。

    只清前端传来的那一个是不够的：前端只知道"自己内存里记过弹窗"的会话，
    刷新过 / 切过页面 / 关过标签页的会话它一无所知 —— 那些中断会留在库里，
    等下次重新打开人工介入时在弹窗里"复活"。
    """
    from app.agent import approvals as approval_mod
    from app.api.routes import approvals as route

    saved = dict(approval_mod._pending_sessions)
    # 登记表是模块级的，前面的用例读过 pending 会往里塞东西 —— 先清干净再测
    approval_mod._pending_sessions.clear()
    # 登记表由 set[str] 变成 dict[session_id, user_id]（user_id=None = 进程级/未知归属）
    approval_mod._pending_sessions.update({"s1": None, "s2": None})
    seen: list[str] = []

    async def fake_resume(sid):
        seen.append(sid)
        return "ok"

    monkeypatch.setattr(route, "_resume_pending", fake_resume)
    try:
        cleared = asyncio.run(route.clear_pending_interrupt(None))
        assert set(seen) == {"s1", "s2"}, "每个登记过的会话都要被收尾"
        assert set(cleared) == {"s1", "s2"}
        assert approval_mod.known_pending_sessions() == [], "收尾成功后要从登记表里摘掉"
    finally:
        approval_mod._pending_sessions.clear()
        approval_mod._pending_sessions.update(saved)


def test_clear_pending_interrupt_scoped_by_user(monkeypatch):
    """按用户收尾 —— 我关自己的审批，不能顺手 reject 掉别人正在等的审批。"""
    from app.agent import approvals as approval_mod
    from app.api.routes import approvals as route

    saved = dict(approval_mod._pending_sessions)
    approval_mod._pending_sessions.clear()
    approval_mod._pending_sessions.update({"mine": "u1", "theirs": "u2"})
    seen: list[str] = []

    async def fake_resume(sid):
        seen.append(sid)
        return "ok"

    monkeypatch.setattr(route, "_resume_pending", fake_resume)
    try:
        cleared = asyncio.run(route.clear_pending_interrupt(None, user_id="u1"))
        assert seen == ["mine"], f"只该收尾 u1 的会话，实际动了 {seen}"
        assert cleared == ["mine"]
        assert approval_mod.known_pending_sessions() == ["theirs"], "别人的登记必须留着"
        assert approval_mod.known_pending_sessions(user_id="u2") == ["theirs"]
    finally:
        approval_mod._pending_sessions.clear()
        approval_mod._pending_sessions.update(saved)


def test_clear_pending_interrupt_handles_errors(monkeypatch):
    """单个会话收尾失败不能连累其它会话。"""
    from app.agent import approvals as approval_mod
    from app.api.routes import approvals as route

    saved = dict(approval_mod._pending_sessions)
    approval_mod._pending_sessions.clear()
    approval_mod._pending_sessions.update({"bad": None, "good": None})

    async def fake_resume(sid):
        if sid == "bad":
            raise RuntimeError("boom")
        return "ok"

    monkeypatch.setattr(route, "_resume_pending", fake_resume)
    try:
        cleared = asyncio.run(route.clear_pending_interrupt(None))
        assert cleared == ["good"]
        assert "bad" in approval_mod.known_pending_sessions(), "失败的会话留着，下次还能再试"
    finally:
        approval_mod._pending_sessions.clear()
        approval_mod._pending_sessions.update(saved)


def test_default_permissions_have_interrupt(monkeypatch, tmp_path):
    """默认权限里的 interrupt 规则**只覆盖本机挂载点**（的审批边界）。

    - 没有本机访问 ⇒ 空列表（工作区不再需要审批）；
    - 有挂载点 ⇒ 每条规则都是 interrupt，且路径落在 `/mnt/...`。
    """
    from app.agent.sandbox import default_permissions
    from app.core.config import settings

    monkeypatch.setattr(settings, "harness_machine_access", False)
    monkeypatch.setattr(settings, "harness_machine_mounts", "")
    assert default_permissions() == [], "没挂本机 ⇒ 没有需要审批的路径"

    host = tmp_path / "host"
    host.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(settings, "harness_machine_access", True)
    monkeypatch.setattr(settings, "harness_machine_mounts", f"tmp={host.as_posix()}")

    perms = default_permissions()
    assert perms, "挂了本机就应至少有一条权限"
    assert all(p.mode == "interrupt" for p in perms)
    assert all(p.paths[0].startswith("/mnt/") for p in perms)
    assert all(p.operations == ["read", "write"] for p in perms)


def test_permission_matches_sensitive_path(monkeypatch, tmp_path):
    """碰本机 → interrupt；碰工作区 → allow（沙箱内的地盘，自由读写）。

    `execute` 不吃路径级权限，所以这里只覆盖文件工具。
    """
    from app.agent.sandbox import default_permissions
    from app.core.config import settings
    from deepagents.middleware.filesystem import _check_fs_permission

    host = tmp_path / "host"
    host.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(settings, "harness_machine_access", True)
    monkeypatch.setattr(settings, "harness_machine_mounts", f"tmp={host.as_posix()}")
    perms = default_permissions()

    # 本机：读和写都要审批
    assert _check_fs_permission(perms, "write", "/mnt/tmp/x.txt") == "interrupt"
    assert _check_fs_permission(perms, "read", "/mnt/tmp/x.txt") == "interrupt"
    # 工作区：完全放行（不再是审批范围）
    assert _check_fs_permission(perms, "write", "/workspace/config.txt") == "allow"
    # 其它虚拟 route：放行
    assert _check_fs_permission(perms, "write", "/memories/x.txt") == "allow"
