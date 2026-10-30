"""跨用户隔离测试（多租户的核心回归）。

## 这个文件在防什么

"加鉴权"只解决"未登录不能用"，**不解决"登录了能不能看别人的"**。
本期真正的风险全在这一层，而且很容易在后续迭代里被悄悄破坏
（新加一个端点忘了归属校验、某个查询忘了加 `WHERE user_id`）。

所以这里用两个账号 **u1 / u2 交叉打每一个以 session_id 入参的端点**，
外加工作区/长期记忆/工具流水/HITL 作用域四类隔离。

## 约定

- 会话直接用 `runtime.create_session(user_id)` 建 —— **不调模型**，
  所以整个文件是秒级的（跑一次真实对话要几十秒，不适合做回归）。
- 所有断言都检查 **404**（不是 403）：后端刻意让"不存在"与"不是你的"
  无法区分，避免攻击者用它枚举别人的 session_id 是否存在。
"""
import asyncio

import pytest


@pytest.fixture(scope="module")
def u1():
    """用户一。

    P18（2026-09-18 调整）：模型改成"每个用户必须自己配"，没有供应商的用户
    连**读自己的历史**都会拿到 400（构建 graph 需要模型）。所以这里给他配一条
    假的私有供应商 —— 它不会被真的调用（这些用例不发消息），只是让 agent 能构建起来。
    （"没有供应商 → 400" 这条单独有用例覆盖，见本文件末尾。）
    """
    from app.core.crypto import encrypt_secret, mask_secret
    from app.db.models import LlmProvider
    from app.db.session import SessionLocal
    from tests.helpers import create_user, unique_email

    user = create_user(email=unique_email("u1"), display_name="用户一")
    db = SessionLocal()
    try:
        db.add(LlmProvider(
            id=f"{user['id'][:24]}00000001",
            user_id=user["id"],
            name="隔离测试用（不会被调用）",
            protocol="openai",
            base_url="https://example.invalid/v1",
            api_key_encrypted=encrypt_secret("sk-isolation-test"),
            api_key_hint=mask_secret("sk-isolation-test"),
            model="test-model",
            parameters="{}",
            is_default=True,
            enabled=True,
        ))
        db.commit()
    finally:
        db.close()
    return user


@pytest.fixture(scope="module")
def u2():
    from tests.helpers import create_user, unique_email

    return create_user(email=unique_email("u2"), display_name="用户二")


@pytest.fixture(scope="module")
def s1(u1):
    """u1 的一个会话（不跑模型）。"""
    from app.agent.runtime import create_session

    return create_session(u1["id"], "u1 的会话")


@pytest.fixture(scope="module")
def client1(u1):
    from tests.helpers import make_auth_client

    client, _ = make_auth_client(u1, raise_server_exceptions=False)
    return client


@pytest.fixture(scope="module")
def client2(u2):
    from tests.helpers import make_auth_client

    client, _ = make_auth_client(u2, raise_server_exceptions=False)
    return client


# ============================================================================
# 1. 会话列表
# ============================================================================


def test_session_list_only_shows_own(client1, client2, s1):
    """u2 的列表里看不到 u1 的会话（本期修的头号问题）。"""
    ids1 = {s["id"] for s in client1.get("/api/v1/sessions").json()}
    ids2 = {s["id"] for s in client2.get("/api/v1/sessions").json()}
    assert s1 in ids1, "u1 应该能看到自己的会话"
    assert s1 not in ids2, "u2 的列表里竟然出现了 u1 的会话"


# ============================================================================
# 2. 以 session_id 入参的端点：u2 一律 404
# ============================================================================

#: (方法, 路径模板, 请求体)。路径里的 {sid} 会被 u1 的会话 id 替换。
CROSS_USER_CASES = [
    ("get", "/api/v1/sessions/{sid}/messages", None),
    ("get", "/api/v1/sessions/{sid}/run", None),
    ("get", "/api/v1/sessions/{sid}/run/stream", None),
    ("post", "/api/v1/sessions/{sid}/stop", None),
    ("patch", "/api/v1/sessions/{sid}", {"title": "改名试试"}),
    ("delete", "/api/v1/sessions/{sid}", None),
    ("get", "/api/v1/approvals/{sid}", None),
    ("post", "/api/v1/approvals/{sid}", {"decision": "approve"}),
    ("post", "/api/v1/chat/stream", {"message": "hi", "session_id": "{sid}"}),
    ("post", "/api/v1/chat", {"message": "hi", "session_id": "{sid}"}),
]


@pytest.mark.parametrize(
    "method,path_tpl,payload",
    CROSS_USER_CASES,
    ids=[f"{m.upper()} {p}" for m, p, _ in CROSS_USER_CASES],
)
def test_u2_cannot_touch_u1_session(client2, s1, method, path_tpl, payload):
    """u2 拿着 u1 的 session_id 打每一个端点 → 全部 404。"""
    path = path_tpl.format(sid=s1)
    body = None
    if payload is not None:
        body = {k: (s1 if v == "{sid}" else v) for k, v in payload.items()}
    resp = getattr(client2, method)(path, json=body) if body is not None else getattr(client2, method)(path)
    assert resp.status_code == 404, (
        f"{method.upper()} {path} 返回了 {resp.status_code}（应 404）：{resp.text[:200]}"
    )
    assert resp.json()["detail"] == "session not found"


def test_u1_can_access_own_session(client1, s1):
    """同一批端点对归属者本人必须正常 —— 否则"隔离"可能只是"全都打不开"。"""
    assert client1.get(f"/api/v1/sessions/{s1}/messages").status_code == 200
    assert client1.get(f"/api/v1/sessions/{s1}/run").status_code == 200
    assert client1.get(f"/api/v1/approvals/{s1}").status_code == 200
    assert client1.patch(f"/api/v1/sessions/{s1}", json={"title": "我自己的标题"}).status_code == 200


def test_deleted_not_found_and_not_owned_are_indistinguishable(client2, u1):
    """"不存在"与"不是你的"必须返回**完全一样**的响应（防枚举）。"""
    from app.agent.runtime import create_session

    real = create_session(u1["id"], "u1 的另一个会话")
    ghost = "0" * 32
    a = client2.get(f"/api/v1/sessions/{real}/messages")
    b = client2.get(f"/api/v1/sessions/{ghost}/messages")
    assert a.status_code == b.status_code == 404
    assert a.json() == b.json()


# ============================================================================
# 3. 工作区（文件）隔离
# ============================================================================


def test_file_workspace_is_per_user(client1, client2, u1, u2):
    """u1 写进工作区的文件，u2 的文件面板里不能出现。"""
    from app.agent.runtime import get_main_backend

    name = "isolation_probe_u1.md"
    path = f"/workspace/{name}"
    backend1 = get_main_backend(user_id=u1["id"])
    asyncio.run(backend1.awrite(path, "这是 u1 的数据"))

    list1 = client1.get("/api/v1/files", params={"path": "/workspace"}).json()
    list2 = client2.get("/api/v1/files", params={"path": "/workspace"}).json()
    paths1 = {e["path"] for e in list1["entries"]}
    paths2 = {e["path"] for e in list2["entries"]}

    assert path in paths1, "u1 应该能在自己的文件面板里看到刚写的文件"
    assert path not in paths2, "u2 竟然看到了 u1 工作区里的文件"

    # u2 就算猜到完整路径也下不到
    assert client2.get("/api/v1/files/download", params={"path": path}).status_code == 404
    assert client1.get("/api/v1/files/download", params={"path": path}).status_code == 200

    # 收尾（别在用户的工作区里留垃圾）
    asyncio.run(backend1.adelete(path))


def test_workspace_roots_are_different(u1, u2):
    """路径层面就不同 —— 这是"物理隔离"，不是靠查询过滤。"""
    from app.core.workspace import workspace_for

    r1, r2 = workspace_for(u1["id"]), workspace_for(u2["id"])
    assert r1 != r2
    assert str(r1).endswith(u1["id"]) and str(r2).endswith(u2["id"])


# ============================================================================
# 4. 长期记忆（Agent.md）隔离
# ============================================================================


def test_long_term_memory_is_per_user(u1, u2):
    """`Agent.md` 必须一人一份 —— 以前它是共享工作区里的一个文件，A 的偏好会进 B 的提示词。"""
    from app.agent.prompt import memory_file_path, memory_sources
    from app.core.workspace import workspace_for

    p1, p2 = memory_file_path(u1["id"]), memory_file_path(u2["id"])
    assert p1 != p2
    assert p1 == workspace_for(u1["id"]) / "Agent.md"

    # 生成并各写各的
    for uid, text in ((u1["id"], "u1 的偏好"), (u2["id"], "u2 的偏好")):
        path = memory_file_path(uid)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(f"# 记忆\n\n{text}\n", encoding="utf-8")

    assert "u1 的偏好" in p1.read_text(encoding="utf-8")
    assert "u1 的偏好" not in p2.read_text(encoding="utf-8")
    assert "u2 的偏好" not in p1.read_text(encoding="utf-8")

    # 注入提示词的**虚拟路径**对谁都一样（由各自的 backend 解析到各自的工作区）
    assert memory_sources(u1["id"]) == memory_sources(u2["id"]) == ["/memories/Agent.md"]


def test_memory_file_not_visible_in_other_users_panel(client2, u1):
    """u2 的文件面板里不该出现 u1 的 Agent.md（它能直接看到别人的偏好）。"""
    from app.agent.prompt import memory_file_path

    memory_file_path(u1["id"]).parent.mkdir(parents=True, exist_ok=True)
    memory_file_path(u1["id"]).write_text("# u1 的私有记忆\n", encoding="utf-8")

    entries = client2.get("/api/v1/files", params={"path": "/workspace"}).json()["entries"]
    names = {e["path"].rsplit("/", 1)[-1] for e in entries}
    # 注意：u2 自己也有一个 Agent.md（种子内容），所以判据是**内容**而不是名字
    for e in entries:
        if e["path"].endswith("/Agent.md"):
            content = client2.get(
                "/api/v1/files/download", params={"path": e["path"]}
            ).content.decode("utf-8")
            assert "u1 的私有记忆" not in content, "u2 读到了 u1 的长期记忆"
    assert len(names) >= 0  # 只是保持上面循环可读性


# ============================================================================
# 5. 工具流水与审计的归属
# ============================================================================


def test_tool_log_written_with_owner(u1):
    """工具流水落库时带上归属（新增列）。"""
    from app.agent.runtime import _log_tool_event
    from app.db.models import ToolCallLog
    from app.db.session import SessionLocal
    from app.agent.runtime import create_session

    sid = create_session(u1["id"], "流水归属测试")
    _log_tool_event(sid, "call", "call_test_1", "read_file", args={"file_path": "/workspace/x"},
                    user_id=u1["id"])
    db = SessionLocal()
    try:
        row = (db.query(ToolCallLog)
               .filter(ToolCallLog.session_id == sid, ToolCallLog.call_id == "call_test_1")
               .one())
        assert row.user_id == u1["id"]
    finally:
        db.close()


# ============================================================================
# 6. HITL 开关作用域
# ============================================================================


def test_hitl_global_switch_requires_admin(client2):
    """普通用户改全局默认 → 403（以前点一下页头开关就改掉所有人的默认值）。"""
    resp = client2.put("/api/v1/hitl/config", json={"enabled": False, "scope": "global"})
    assert resp.status_code == 403
    assert "管理员" in resp.json()["detail"]


def test_hitl_user_scope_does_not_affect_others(client1, client2, u1, u2):
    """u2 关掉自己的人工介入，不影响 u1 的有效值。"""
    from app.agent import permissions

    before_u1 = permissions.hitl_enabled(None, u1["id"])

    resp = client2.put("/api/v1/hitl/config", json={"enabled": False})  # 默认 scope=user
    assert resp.status_code == 200, resp.text
    assert permissions.hitl_enabled(None, u2["id"]) is False
    assert permissions.hitl_enabled(None, u1["id"]) == before_u1, "u2 的切换泄漏到了 u1"

    # 复原（进程内状态，避免影响同一次 pytest 里的其它用例）
    permissions.set_hitl(before_u1, None, u2["id"])


def test_hitl_session_scope_requires_ownership(client2, s1):
    """u2 不能拿 u1 的会话去改开关。"""
    resp = client2.put("/api/v1/hitl/config", json={"enabled": True, "session_id": s1})
    assert resp.status_code == 404
    assert client2.get("/api/v1/hitl/config", params={"session_id": s1}).status_code == 404


def test_hitl_admin_can_set_global(admin_client):
    """管理员改全局默认是允许的（并且要能改回来）。"""
    from app.agent import permissions

    before = permissions.hitl_state(None, None)["global_default"]
    try:
        resp = admin_client.put("/api/v1/hitl/config", json={"enabled": not before, "scope": "global"})
        assert resp.status_code == 200, resp.text
        assert resp.json()["global_default"] is (not before)
    finally:
        admin_client.put("/api/v1/hitl/config", json={"enabled": before, "scope": "global"})


# ============================================================================
# 7. 系统级资源（技能定义）仅管理员可写
# ============================================================================


def test_skill_write_requires_admin(client2, admin_client):
    """技能定义是系统级 YAML（所有人共享），所以写操作仅管理员。

    判据用"是否被**权限**拦住"而不是最终状态码：普通用户要拿到 403，
    管理员则应该通过鉴权、落到"参数校验失败"那一层（400）。
    """
    body = {"name": "Bad Name!", "description": "x", "instructions": "y"}
    assert client2.post("/api/v1/skills/", json=body).status_code == 403
    assert admin_client.post("/api/v1/skills/", json=body).status_code == 400


def test_skill_list_readable_by_any_user(client1, client2):
    """列技能名任何登录用户都能看（它本来就会出现在每个人的提示词里）。"""
    assert client1.get("/api/v1/skills/").status_code == 200
    assert client2.get("/api/v1/skills/").status_code == 200


# ============================================================================
# 8. 没配模型的用户：明确 400，而不是 500
# ============================================================================


def test_user_without_provider_gets_400_not_500():
    """"还没配置模型"必须是 **400 + 可操作的中文**，而不是 500"服务端错误"。

    为什么这条值得单独测：它决定用户看到的是"你去配一下模型"还是"服务端炸了"。
    而且它会在**多个入口**出现（发消息、读历史、看审批），所以要一次性验证
    "全局那一处兜住了所有入口"，而不是逐个路由各写一遍（写漏一个就漏一个）。
    """
    from app.agent.runtime import create_session
    from tests.helpers import create_user, make_auth_client, unique_email

    # 这个账号没有任何供应商（而且现在也不会再回退到别人的/系统级的）
    user = create_user(email=unique_email("nomodel"), display_name="没配模型")
    client, _ = make_auth_client(user, raise_server_exceptions=False)
    sid = create_session(user["id"], "没配模型也能建会话")

    # 发消息
    r = client.post("/api/v1/chat", json={"message": "hi", "session_id": sid})
    assert r.status_code == 400, f"发消息应 400（实际 {r.status_code}）：{r.text[:200]}"
    assert "模型" in r.json()["detail"]

    # History and pending-state reads are offline operations: users can still
    # inspect old conversations after removing or disabling their provider.
    r = client.get(f"/api/v1/sessions/{sid}/messages")
    assert r.status_code == 200 and r.json() == []
    r = client.get(f"/api/v1/approvals/{sid}")
    assert r.status_code == 200 and not r.json()["pending"]

    # 而且他的列表里是空的 —— 看不到别人的模型配置
    assert client.get("/api/v1/llm/providers").json() == []
