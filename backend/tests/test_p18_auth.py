"""认证流程测试。

覆盖：未登录 401（含"所有业务端点都受保护"的结构性断言）、注册、登录、
当前用户、口令校验、token 过期/伪造、禁用账号、注册开关、改口令，
以及"口令在库里是 argon2 哈希、任何接口都不回明文"。

⚠️ 这些用例连的是**真实 MySQL**（本项目既有约定：`app.db.session.engine` 在
import 时就绑定 `settings.mysql_url`）。所有测试账号都用 `@p18.test` 域名，
由 `conftest._purge_test_accounts` 在 session 结束时统一删除。
"""
import uuid

import pytest

from app.core.config import settings
from app.core.security import create_access_token, hash_password
from app.db.models import User
from app.db.session import SessionLocal
from tests.helpers import TEST_EMAIL_DOMAIN, TEST_PASSWORD, create_user, unique_email


# ============================================================================
# 1. 默认拒绝：未登录一律 401
# ============================================================================

#: 受保护的端点（方法, 路径, 期望 payload）。
#: 请求体一律给"空/非法"，因为依赖先于请求体校验执行 —— 所以能拿到 401
#: 就说明鉴权真的生效，而且不会产生任何副作用。
PROTECTED_ENDPOINTS = [
    ("GET", "/api/v1/sessions", None),
    ("GET", "/api/v1/sessions/bogus/messages", None),
    ("GET", "/api/v1/sessions/bogus/run", None),
    ("GET", "/api/v1/sessions/bogus/run/stream", None),
    ("POST", "/api/v1/sessions/bogus/stop", None),
    ("DELETE", "/api/v1/sessions/bogus", None),
    ("PATCH", "/api/v1/sessions/bogus", {"title": "x"}),
    ("GET", "/api/v1/files", None),
    ("GET", "/api/v1/files/download", None),
    ("DELETE", "/api/v1/files", None),
    ("GET", "/api/v1/approvals/bogus", None),
    ("POST", "/api/v1/approvals/bogus", {"decision": "approve"}),
    ("GET", "/api/v1/hitl/config", None),
    ("PUT", "/api/v1/hitl/config", {"enabled": True}),
    # 权限模式（默认权限（沙箱内）/ 完全访问）
    ("GET", "/api/v1/access/config", None),
    ("PUT", "/api/v1/access/config", {"mode": "full"}),
    ("POST", "/api/v1/chat", {"message": "hi"}),
    ("POST", "/api/v1/chat/stream", {"message": "hi"}),
    ("GET", "/api/v1/skills/", None),
    ("POST", "/api/v1/skills/", {}),
]


@pytest.mark.parametrize("method,path,payload", PROTECTED_ENDPOINTS,
                         ids=[f"{m} {p}" for m, p, _ in PROTECTED_ENDPOINTS])
def test_protected_endpoint_requires_login(anon_client, method, path, payload):
    """未登录访问任何业务端点 → 401。"""
    resp = anon_client.request(method, path, json=payload)
    assert resp.status_code == 401, f"{method} {path} 竟然返回 {resp.status_code}：{resp.text[:200]}"
    assert "未登录" in resp.json()["detail"]
    assert resp.headers.get("www-authenticate") == "Bearer"


def test_openapi_declares_authorization_for_all_business_paths():
    """结构性断言：**每个**业务端点都必须声明 `authorization` 头。

    这条用例的价值在于"防回归"：将来有人新增路由时若不小心把它挂在
    `api_router` 之外，这里立刻红 —— 比逐个手写 401 用例可靠得多。
    """
    from app.main import app

    missing = []
    for path, ops in app.openapi()["paths"].items():
        if not path.startswith("/api/v1") or path.startswith("/api/v1/auth"):
            continue
        for method, op in ops.items():
            names = [p.get("name") for p in op.get("parameters", [])]
            if "authorization" not in names:
                missing.append(f"{method.upper()} {path}")
    assert not missing, f"以下业务端点没有要求登录：{missing}"


def test_openapi_auth_paths_do_not_require_authorization():
    """反向断言：auth 端点**不能**要求登录（否则登录接口本身登不进去）。"""
    from app.main import app

    paths = app.openapi()["paths"]
    for path in ("/api/v1/auth/login", "/api/v1/auth/register"):
        for method, op in paths[path].items():
            names = [p.get("name") for p in op.get("parameters", [])]
            assert "authorization" not in names, f"{method.upper()} {path} 不该要求登录"


def test_public_endpoints_reachable_without_login(anon_client):
    """/health 与 / 保持公开（探活与文档入口不该要凭证）。"""
    assert anon_client.get("/health").status_code == 200
    assert anon_client.get("/").status_code == 200


# ============================================================================
# 2. 注册 / 登录 / 当前用户
# ============================================================================


def test_register_login_me_flow(anon_client):
    """完整闭环：注册 → 登录 → 带 token 取 me。"""
    email = unique_email("flow")
    resp = anon_client.post(
        "/api/v1/auth/register",
        json={"email": email, "password": TEST_PASSWORD, "display_name": "闭环用户"},
    )
    assert resp.status_code == 201, resp.text
    created = resp.json()
    assert created["email"] == email
    assert created["role"] == "user"
    assert created["display_name"] == "闭环用户"
    assert "password" not in str(created) and "hash" not in str(created)

    resp = anon_client.post(
        "/api/v1/auth/login", json={"email": email, "password": TEST_PASSWORD}
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["token_type"] == "bearer"
    assert body["expires_in"] == settings.access_token_expire_minutes * 60
    assert body["user"]["id"] == created["id"]
    token = body["access_token"]

    resp = anon_client.get("/api/v1/auth/me", headers={"Authorization": f"Bearer {token}"})
    assert resp.status_code == 200
    assert resp.json()["id"] == created["id"]


def test_register_email_is_case_insensitive(anon_client):
    """`A@x` 与 `a@x` 必须视为同一账号（否则能注册出一堆重复用户）。"""
    email = unique_email("case")
    upper = email.upper()
    assert anon_client.post(
        "/api/v1/auth/register", json={"email": upper, "password": TEST_PASSWORD}
    ).status_code == 201
    # 用不同大小写再注册一次 → 409
    resp = anon_client.post(
        "/api/v1/auth/register", json={"email": email, "password": TEST_PASSWORD}
    )
    assert resp.status_code == 409
    # 用原始大小写也能登录
    assert anon_client.post(
        "/api/v1/auth/login", json={"email": upper, "password": TEST_PASSWORD}
    ).status_code == 200


def test_register_duplicate_email_409(anon_client):
    email = unique_email("dup")
    assert anon_client.post(
        "/api/v1/auth/register", json={"email": email, "password": TEST_PASSWORD}
    ).status_code == 201
    resp = anon_client.post(
        "/api/v1/auth/register", json={"email": email, "password": TEST_PASSWORD}
    )
    assert resp.status_code == 409
    assert "已被注册" in resp.json()["detail"]


def test_register_invalid_email_422(anon_client):
    for bad in ("noatsign", "a b@c.com", "@x.com", "x@", ""):
        resp = anon_client.post(
            "/api/v1/auth/register", json={"email": bad, "password": TEST_PASSWORD}
        )
        assert resp.status_code == 422, f"{bad!r} 竟然通过了：{resp.text[:120]}"


def test_register_short_password_422(anon_client):
    resp = anon_client.post(
        "/api/v1/auth/register", json={"email": unique_email("short"), "password": "1234"}
    )
    assert resp.status_code == 422


def test_register_respects_allow_registration_flag(anon_client, monkeypatch):
    """`ALLOW_REGISTRATION=false` 时自助注册必须被拒（403）。"""
    monkeypatch.setattr(settings, "allow_registration", False)
    resp = anon_client.post(
        "/api/v1/auth/register", json={"email": unique_email("closed"), "password": TEST_PASSWORD}
    )
    assert resp.status_code == 403
    assert "关闭自助注册" in resp.json()["detail"]


# ============================================================================
# 3. 登录失败路径
# ============================================================================


def test_login_wrong_password_401(anon_client, auth_user):
    resp = anon_client.post(
        "/api/v1/auth/login", json={"email": auth_user["email"], "password": "WrongPassw0rd!"}
    )
    assert resp.status_code == 401


def test_login_unknown_email_gives_same_message(anon_client, auth_user):
    """账号不存在与口令错误必须给**完全相同**的响应（防账号枚举）。"""
    wrong_pwd = anon_client.post(
        "/api/v1/auth/login", json={"email": auth_user["email"], "password": "WrongPassw0rd!"}
    )
    no_user = anon_client.post(
        "/api/v1/auth/login",
        json={"email": f"nobody_{uuid.uuid4().hex[:8]}@{TEST_EMAIL_DOMAIN}", "password": "WrongPassw0rd!"},
    )
    assert wrong_pwd.status_code == no_user.status_code == 401
    assert wrong_pwd.json()["detail"] == no_user.json()["detail"] == "邮箱或口令不正确"


def test_login_inactive_user_401(anon_client):
    """被禁用的账号不能登录（口令对也不行）。"""
    user = create_user(is_active=False)
    resp = anon_client.post(
        "/api/v1/auth/login", json={"email": user["email"], "password": user["password"]}
    )
    assert resp.status_code == 401
    assert "禁用" in resp.json()["detail"]


# ============================================================================
# 4. token 有效性
# ============================================================================


def test_expired_token_401(anon_client, auth_user):
    """过期的 token → 401，且提示是"过期"而不是"无效"（前端好给准确文案）。"""
    token, _ = create_access_token(auth_user["id"], expires_minutes=-1)
    resp = anon_client.get("/api/v1/auth/me", headers={"Authorization": f"Bearer {token}"})
    assert resp.status_code == 401
    assert "过期" in resp.json()["detail"]


def test_forged_and_malformed_tokens_401(anon_client, auth_user):
    """伪造签名 / 乱码 / 缺头 / 格式不对，一律 401。"""
    cases = [
        {"Authorization": "Bearer eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiJ4In0.forged"},
        {"Authorization": "Bearer not-a-jwt-at-all"},
        {"Authorization": "Basic YWRtaW46YWRtaW4="},
        {"Authorization": "Bearer"},
        {"Authorization": "   "},
        {},
    ]
    for headers in cases:
        resp = anon_client.get("/api/v1/auth/me", headers=headers)
        assert resp.status_code == 401, f"{headers} 竟然返回 {resp.status_code}"


def test_token_for_deleted_user_401(anon_client):
    """token 签名有效但用户已被删 → 401（不是 500）。"""
    user = create_user()
    token, _ = create_access_token(user["id"])
    db = SessionLocal()
    try:
        db.query(User).filter(User.id == user["id"]).delete()
        db.commit()
    finally:
        db.close()
    assert anon_client.get(
        "/api/v1/auth/me", headers={"Authorization": f"Bearer {token}"}
    ).status_code == 401


def test_valid_token_for_inactive_user_401(anon_client):
    """签发后被禁用的账号：持有效 token 也应该被拦下。"""
    user = create_user()
    token, _ = create_access_token(user["id"])
    db = SessionLocal()
    try:
        db.query(User).filter(User.id == user["id"]).update({"is_active": False})
        db.commit()
    finally:
        db.close()
    resp = anon_client.get("/api/v1/auth/me", headers={"Authorization": f"Bearer {token}"})
    assert resp.status_code == 401
    assert "禁用" in resp.json()["detail"]


# ============================================================================
# 5. 口令存储与泄漏
# ============================================================================


def test_password_stored_as_argon2_hash(anon_client):
    """库里必须是 argon2id 哈希，绝不能出现明文。"""
    email = unique_email("hash")
    password = "Sup3rSecret!Pass"
    assert anon_client.post(
        "/api/v1/auth/register", json={"email": email, "password": password}
    ).status_code == 201

    db = SessionLocal()
    try:
        user = db.query(User).filter(User.email == email).one()
        assert user.password_hash.startswith("$argon2id$"), user.password_hash[:30]
        assert user.password_hash != password
        assert password not in user.password_hash
    finally:
        db.close()


def test_auth_responses_never_leak_password_material(anon_client, auth_user):
    """注册/登录/me 的响应体里不能出现口令或哈希的任何痕迹。"""
    secret = "ResponseLeak!Check1"
    email = unique_email("leak")
    reg = anon_client.post("/api/v1/auth/register", json={"email": email, "password": secret})
    login = anon_client.post("/api/v1/auth/login", json={"email": email, "password": secret})
    me = anon_client.get(
        "/api/v1/auth/me",
        headers={"Authorization": f"Bearer {login.json()['access_token']}"},
    )
    db = SessionLocal()
    try:
        stored_hash = db.query(User).filter(User.email == email).one().password_hash
    finally:
        db.close()

    for resp in (reg, login, me):
        body = resp.text
        assert secret not in body
        assert stored_hash not in body
        assert "password_hash" not in body


# ============================================================================
# 6. 改口令
# ============================================================================


def test_change_password_flow(anon_client):
    """旧口令校验 → 换新 → 新口令能登、旧口令不能登。

    用**独立账号**而不是共享的 `auth_user`：这条用例会改口令，
    万一中途断言失败没走到"复原"，共享账号会把同一 session 里的其它用例一起带崩。
    """
    user = create_user()
    old, new = user["password"], "BrandNew!Passw0rd"
    login = anon_client.post("/api/v1/auth/login", json={"email": user["email"], "password": old})
    token = login.json()["access_token"]
    h = {"Authorization": f"Bearer {token}"}

    # 旧口令给错 → 401
    assert anon_client.post(
        "/api/v1/auth/change-password",
        json={"old_password": "Nope!12345678", "new_password": new},
        headers=h,
    ).status_code == 401
    # 新旧相同 → 422
    assert anon_client.post(
        "/api/v1/auth/change-password",
        json={"old_password": old, "new_password": old},
        headers=h,
    ).status_code == 422
    # 正常修改 → 200
    assert anon_client.post(
        "/api/v1/auth/change-password",
        json={"old_password": old, "new_password": new},
        headers=h,
    ).status_code == 200

    assert anon_client.post(
        "/api/v1/auth/login", json={"email": user["email"], "password": new}
    ).status_code == 200
    assert anon_client.post(
        "/api/v1/auth/login", json={"email": user["email"], "password": old}
    ).status_code == 401
    # 改口令**不会**让已签发的 token 失效（无状态 JWT 的固有语义）：
    # 所以这里不校验旧 token 失效 —— 要立刻踢下线得引入 token 版本号，
    # 那属于另一件事（本期不做，已记录在方案里）。
    assert anon_client.get("/api/v1/auth/me", headers=h).status_code == 200


def test_change_password_requires_login(anon_client):
    resp = anon_client.post(
        "/api/v1/auth/change-password",
        json={"old_password": "x", "new_password": "y"},
    )
    assert resp.status_code == 401
