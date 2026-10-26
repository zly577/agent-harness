"""测试：权限模式（默认权限（沙箱内） / 完全访问）。

覆盖：
1. `access` 注册表 —— 归一化校验、三级作用域解析、状态输出；
2. `build_main_backend(mode="full")` → `HostShellBackend`（deepagents `LocalShellBackend`
   子类），文件层与 sandbox 模式**完全一致**，execute 在本机跑；
3. 审批边界 —— full 模式一律不审批；sandbox 模式只审批本机 `/mnt/**`；
   **空规则时仍要给占位 interrupt_on**（保住图结构，见 `permissions.py`）；
4. 提示词 —— 两种模式的工具说明段互斥，不会同时出现两套矛盾说法；
5. API —— GET/PUT /api/v1/access/config（作用域、非法的 400、非 admin 改全局 403）。

不联网、不建真容器：sandbox 模式只做结构断言；full 模式的 execute 只跑
`echo` 这类绝对安全的命令。
"""
from __future__ import annotations

import os
import re
from pathlib import Path

import pytest

from app.agent import access, permissions, sandbox


@pytest.fixture()
def tmp_path(request):
    """覆写 pytest tmp_path：用工作区内目录，避免受限沙箱下 rmtree 失败（同）。"""
    safe = re.sub(r"[^A-Za-z0-9_.-]+", "_", request.node.name)[:60]
    base = Path(__file__).resolve().parent / "_tmp_hybrid" / f"p21_{safe}"
    base.mkdir(parents=True, exist_ok=True)
    return base


@pytest.fixture()
def workspace(tmp_path):
    ws = tmp_path / "ws"
    ws.mkdir(parents=True, exist_ok=True)
    return ws


@pytest.fixture(autouse=True)
def _restore_access_mode():
    """每个用例前后都把模式恢复成全局 sandbox —— 注册表是模块级内存态，会串台。"""
    saved_user = dict(access._user_mode)
    saved_session = dict(access._session_mode)
    saved_global = access._global_mode
    try:
        yield
    finally:
        access._user_mode.clear()
        access._user_mode.update(saved_user)
        access._session_mode.clear()
        access._session_mode.update(saved_session)
        access._global_mode = saved_global


@pytest.fixture()
def no_machine(monkeypatch):
    from app.core.config import settings

    monkeypatch.setattr(settings, "harness_machine_access", False)
    monkeypatch.setattr(settings, "harness_machine_mounts", "")


# --------------------------------------------------------------------------
# 1. 模式注册表
# --------------------------------------------------------------------------


def test_normalize_mode_rejects_unknown():
    """拼错的模式名必须报错，不能静默当成 sandbox（否则"完全访问没生效"无从发现）。"""
    assert access.normalize_mode("SANDBOX") == access.MODE_SANDBOX
    assert access.normalize_mode(" Full ") == access.MODE_FULL
    with pytest.raises(ValueError):
        access.normalize_mode("local")
    with pytest.raises(ValueError):
        access.normalize_mode("")


def test_deployment_can_disable_full_access(monkeypatch):
    """即使有旧的 full 覆盖，部署禁用开关也必须生效。"""
    access.set_access_mode(access.MODE_FULL, user_id="docker-user")
    monkeypatch.setattr(access.settings, "harness_allow_full_access", False)
    assert access.access_mode(user_id="docker-user") == access.MODE_SANDBOX
    with pytest.raises(ValueError, match="禁用完全访问"):
        access.set_access_mode(access.MODE_FULL, user_id="docker-user")


def test_disabled_full_access_rejected_by_api(monkeypatch):
    """已登录的用户也不能通过 HTTP 绕过部署策略；无需数据库连接。"""
    from fastapi.testclient import TestClient
    from app.api.deps import get_current_user
    from app.db.models import User
    from app.db.session import get_db
    from app.main import app

    monkeypatch.setattr(access.settings, "harness_allow_full_access", False)
    overrides = dict(app.dependency_overrides)
    app.dependency_overrides[get_current_user] = lambda: User(
        id="docker-user", email="docker@example.com", role="user"
    )
    app.dependency_overrides[get_db] = lambda: None
    try:
        response = TestClient(app).put("/api/v1/access/config", json={"mode": "full"})
        assert response.status_code == 400
        assert "禁用完全访问" in response.json()["detail"]
        assert access.access_mode(user_id="docker-user") == access.MODE_SANDBOX
    finally:
        app.dependency_overrides.clear()
        app.dependency_overrides.update(overrides)


def test_mode_scope_precedence():
    """优先级：会话 → 用户 → 全局默认。"""
    access.set_access_mode(access.MODE_SANDBOX)  # 全局
    assert access.access_mode() == access.MODE_SANDBOX
    assert access.access_mode(user_id="u1") == access.MODE_SANDBOX

    access.set_access_mode(access.MODE_FULL, user_id="u1")
    assert access.access_mode(user_id="u1") == access.MODE_FULL
    assert access.access_mode(user_id="u2") == access.MODE_SANDBOX, "别人的默认值不受影响"

    access.set_access_mode(access.MODE_SANDBOX, session_id="s1")
    assert access.access_mode("s1", "u1") == access.MODE_SANDBOX, "会话覆盖优先于用户"

    # 改全局会清掉所有覆盖（语义同 HITL 开关）
    access.set_access_mode(access.MODE_FULL)
    assert access.access_mode("s1", "u1") == access.MODE_FULL
    assert access.access_state()["user_overrides"] == {}
    assert access.access_state()["session_overrides"] == {}


def test_access_state_shape():
    access.set_access_mode(access.MODE_SANDBOX)
    st = access.access_state()
    assert st["mode"] == access.MODE_SANDBOX
    assert st["full_access"] is False
    assert st["scope"] == "global"
    assert st["valid_modes"] == [access.MODE_SANDBOX, access.MODE_FULL]
    assert st["label"]


# --------------------------------------------------------------------------
# 2. 两种模式的后端
# --------------------------------------------------------------------------


def test_full_mode_uses_local_shell_backend(workspace, no_machine):
    """full 模式：default 是 HostShellBackend（LocalShellBackend 子类），且仍可执行。"""
    from deepagents.backends import LocalShellBackend
    from deepagents.middleware.filesystem import supports_execution

    be = sandbox.build_main_backend(session_key="t", workspace=str(workspace), mode="full")
    assert isinstance(be.default, sandbox.HostShellBackend)
    assert isinstance(be.default, LocalShellBackend), "要求直接使用 deepagents 自带后端"
    assert supports_execution(be) is True, "具备执行能力 ⇒ 才会注册 execute 工具"
    assert sandbox.PERMISSION_ROUTE_PREFIX in be.routes
    assert be.routes[sandbox.PERMISSION_ROUTE_PREFIX] is be.default


def test_sandbox_mode_uses_hybrid_backend(workspace, no_machine):
    """sandbox 模式（默认）：default 仍是 HybridDefaultBackend，行为不变。"""
    from deepagents.middleware.filesystem import supports_execution

    be = sandbox.build_main_backend(session_key="t", workspace=str(workspace))
    assert isinstance(be.default, sandbox.HybridDefaultBackend)
    assert supports_execution(be) is True


def test_file_layer_identical_across_modes(workspace, no_machine):
    """两种模式的**文件层必须一致**：同一路径、同一结果（只有执行层不同）。"""
    full = sandbox.build_main_backend(session_key="t", workspace=str(workspace), mode="full")
    sbx = sandbox.build_main_backend(session_key="t", workspace=str(workspace), mode="sandbox")

    assert full.write("/workspace/a/b.txt", "hi").error is None
    # sandbox 模式读的是同一个物理文件（两边的虚拟根都指向 workspace）
    assert sbx.read("/workspace/a/b.txt").file_data["content"] == "hi"
    assert (workspace / "a" / "b.txt").read_text(encoding="utf-8") == "hi"
    # ls / glob 的路径写法也一致
    assert [e["path"] for e in full.ls("/workspace").entries] == [
        e["path"] for e in sbx.ls("/workspace").entries
    ]


def test_full_mode_execute_runs_on_host(workspace, no_machine, monkeypatch):
    """full 模式的 execute 直接在本机跑：工作目录 = 工作区，不走沙箱。

    这里同时是"沙箱没有被碰过"的证明 —— 用一个会立刻报错的假 registry，
    如果代码误走沙箱路径，用例会直接炸。
    """
    from app.agent import opensandbox_backend as osb

    def _boom(*a, **kw):  # pragma: no cover - 走到就说明路由错了
        raise AssertionError("full 模式不应该访问沙箱")

    monkeypatch.setattr(osb.registry, "get", _boom)

    be = sandbox.build_main_backend(session_key="t", workspace=str(workspace), mode="full")
    out = be.execute("echo p21-host")
    assert out.exit_code == 0
    assert "p21-host" in (out.output or "")

    # 工作目录 = 工作区：写个文件再让 shell 找它，能证明"命令在同一个目录里"
    (workspace / "marker.txt").write_text("found", encoding="utf-8")
    cat = "type marker.txt" if os.name == "nt" else "cat marker.txt"
    res = be.execute(cat)
    assert res.exit_code == 0 and "found" in (res.output or "")


# --------------------------------------------------------------------------
# 3. 审批边界
# --------------------------------------------------------------------------


def test_full_mode_never_interrupts(workspace, no_machine):
    """full 模式：规则为空，且仍给占位 interrupt_on（图结构不变）。"""
    assert sandbox.default_permissions("full") == []
    assert permissions.agent_permissions(None, None, "full") == []
    assert permissions.hitl_interrupt_on(None, None, "full") == {
        permissions.DISABLED_INTERRUPT_KEY: False
    }


def test_sandbox_mode_only_gates_machine_paths(workspace, monkeypatch):
    """sandbox 模式：只有本机 `/mnt/**` 需要审批；工作区自由。"""
    from app.agent import opensandbox_backend as osb
    from app.core.config import settings

    host = workspace / "host"
    (host / "proj").mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(settings, "harness_machine_access", True)
    monkeypatch.setattr(settings, "harness_machine_mounts", f"tmp={host.as_posix()}")

    be = sandbox.build_main_backend(session_key="t", workspace=str(workspace), mode="sandbox")
    perms = sandbox.default_permissions("sandbox")
    assert [p for rule in perms for p in rule.paths] == ["/mnt/tmp/**"]
    assert all(rule.operations == ["read", "write"] for rule in perms)

    # 有规则时审批表交给 permissions 生成（返回 None）
    permissions.set_hitl(True)
    permissions._session_hitl.clear()
    permissions._user_hitl.clear()
    try:
        assert permissions.hitl_interrupt_on(None, None, "sandbox") is None
        assert permissions.agent_permissions(None, None, "sandbox")
    finally:
        permissions.set_hitl(True)
        permissions._user_hitl.clear()
        permissions._session_hitl.clear()

    # 本机文件确实可读（审批是"拦截层"，不改变文件层能力）
    (host / "proj" / "x.txt").write_text("local", encoding="utf-8")
    assert be.read("/mnt/tmp/proj/x.txt").file_data["content"] == "local"
    assert osb.machine_mounts()


def test_sandbox_mode_read_check_is_interrupt(monkeypatch, tmp_path):
    """读本机文件也要审批 —— 需求原文是"如果需要**访问**本地，那就进入 HITL"。"""
    from app.core.config import settings
    from deepagents.middleware.filesystem import _check_fs_permission

    host = tmp_path / "host"
    host.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(settings, "harness_machine_access", True)
    monkeypatch.setattr(settings, "harness_machine_mounts", f"tmp={host.as_posix()}")

    perms = sandbox.default_permissions("sandbox")
    assert _check_fs_permission(perms, "read", "/mnt/tmp/a.txt") == "interrupt"
    assert _check_fs_permission(perms, "write", "/mnt/tmp/a.txt") == "interrupt"
    assert _check_fs_permission(perms, "write", "/workspace/a.txt") == "allow"


# --------------------------------------------------------------------------
# 3.5 图装配（最容易出问题的一步：backend 换成 HostShellBackend 之后
#     deepagents 的 route / 权限校验、子智能体的 FilesystemMiddleware 都要照常通过）
# --------------------------------------------------------------------------


def test_create_deep_agent_full_mode_with_subagents(workspace, no_machine, monkeypatch):
    """端到端装配回归：完全访问模式 + 子智能体 + 空权限，必须能建出图。

    三个容易炸的点，这条用例把它们一起钉住：

    1. `permissions=[]` 时 deepagents 会跳过 route 校验 —— 但**子智能体**的
       `permissions_allow_read: ["/"]` 会走校验，用的是父级（可执行）backend；
    2. 没有任何 interrupt 规则时，`interrupt_on` 必须给占位键，
       否则 `HumanInTheLoopMiddleware` 不进图（见 `permissions.py` 的头注释）；
    3. `execute` 工具必须照常注册 —— 它现在路由到本机 shell。
    """
    monkeypatch.setenv("DEEPSEEK_API_KEY", "sk-test-placeholder")
    monkeypatch.setattr("app.agent.model.settings.deepseek_api_key", "sk-test-placeholder")
    from deepagents import create_deep_agent

    from app.agent.model import build_llm
    from app.agent.subagents import load_all_subagents

    be = sandbox.build_main_backend(session_key="t", workspace=str(workspace), mode="full")
    assert sandbox.validate_permissions(be, mode="full") == []

    agent = create_deep_agent(
        model=build_llm(),
        name="t",
        backend=be,
        permissions=permissions.agent_permissions(None, None, "full"),
        interrupt_on=permissions.hitl_interrupt_on(None, None, "full"),
        subagents=load_all_subagents(),
    )
    tools = set(agent.nodes["tools"].bound._tools_by_name)
    assert {"execute", "write_file", "read_file", "task"} <= tools

    # 图结构：HITL 节点必须在（完全访问只是"不拦"，不是"拆掉审批节点"）
    assert any("HumanInTheLoop" in str(n) for n in agent.nodes), (
        f"完全访问模式下 HITL 节点也必须存在，实际节点：{list(agent.nodes)}"
    )


# --------------------------------------------------------------------------
# 4. 提示词
# --------------------------------------------------------------------------


def test_prompts_are_mutually_exclusive():
    """两套工具说明段不能同时出现（否则模型会同时读到矛盾事实）。"""
    from app.agent.prompt import build_system_prompt, full_access_prompt_section

    sbx = build_system_prompt()
    full = build_system_prompt(mode="full")

    assert "隔离沙箱容器" in sbx
    assert "隔离沙箱容器" not in full
    assert "没有沙箱" in full
    assert "完全访问模式" in full
    assert "完全访问模式" not in sbx
    # execute 的路径写法必须交代清楚（这是历史上"空转几十次"的主因）
    assert "/mnt/" in full_access_prompt_section()
    assert "execute" in full_access_prompt_section()
    # 默认参数 = sandbox，老调用点行为不变
    assert build_system_prompt() == build_system_prompt(mode="sandbox")


# --------------------------------------------------------------------------
# 5. API
# --------------------------------------------------------------------------


def test_access_config_api_requires_login(anon_client):
    assert anon_client.get("/api/v1/access/config").status_code == 401


def test_access_config_api_roundtrip(auth_client):
    """GET 看状态、PUT 切模式（用户级），再 GET 复查。"""
    resp = auth_client.put("/api/v1/access/config", json={"mode": "full"})
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["mode"] == "full"
    assert body["full_access"] is True
    assert body["scope"] == "user"

    again = auth_client.get("/api/v1/access/config")
    assert again.status_code == 200
    assert again.json()["mode"] == "full"

    # 收尾：切回沙箱，避免污染同 session 的其他用例
    back = auth_client.put("/api/v1/access/config", json={"mode": "sandbox"})
    assert back.status_code == 200
    assert back.json()["mode"] == "sandbox"


def test_access_config_api_rejects_bad_mode(auth_client):
    resp = auth_client.put("/api/v1/access/config", json={"mode": "local"})
    assert resp.status_code == 400
    assert "可选" in resp.json()["detail"]


def test_access_config_api_global_needs_admin(auth_client, admin_client):
    """普通用户改全局 → 403；admin 可以。"""
    denied = auth_client.put(
        "/api/v1/access/config", json={"mode": "full", "scope": "global"}
    )
    assert denied.status_code == 403

    ok = admin_client.put(
        "/api/v1/access/config", json={"mode": "sandbox", "scope": "global"}
    )
    assert ok.status_code == 200
    assert ok.json()["scope"] == "global"
