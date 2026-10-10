"""测试辅助：认证账号与带 token 的客户端。

## 为什么测试账号直接写库，而不是走注册接口

注册接口受配置 `ALLOW_REGISTRATION` 控制，而 `test_p18_auth.py` 里有专门用例
把它关掉再验一次 —— 如果其它测试都要靠"注册接口"来准备账号，它们就会跟着这条
配置一起翻车。所以这里**直接建账号**（只依赖 ORM 与口令哈希），
把"注册接口本身对不对"留给它自己的用例。

## 清理

所有测试账号都用 `@p18.test` 域名，`purge_test_users()` 在 session 结束时
一次删干净（含它们的会话）。这样跑测试不会往用户的真实库里留垃圾。
"""
from __future__ import annotations

import uuid

from fastapi.testclient import TestClient

from app.core.security import hash_password
from app.db.models import Session as DbSession
from app.db.models import User
from app.db.session import SessionLocal
from app.main import app

#: 测试账号专用域名（清理时按它匹配）
TEST_EMAIL_DOMAIN = "p18.test"

#: 测试账号统一口令
TEST_PASSWORD = "TestPassw0rd!"


def unique_email(prefix: str = "u") -> str:
    """生成一个不会撞车的测试邮箱。"""
    return f"{prefix}_{uuid.uuid4().hex[:10]}@{TEST_EMAIL_DOMAIN}"


def create_user(
    email: str | None = None,
    password: str = TEST_PASSWORD,
    *,
    display_name: str = "测试用户",
    role: str = "user",
    is_active: bool = True,
) -> dict:
    """直接在库里建一个账号。

    Returns:
        `{"id", "email", "password", "role"}`
    """
    email = (email or unique_email()).lower()
    user_id = uuid.uuid4().hex
    db = SessionLocal()
    try:
        db.add(User(
            id=user_id,
            email=email,
            password_hash=hash_password(password),
            display_name=display_name,
            role=role,
            is_active=is_active,
        ))
        db.commit()
    finally:
        db.close()
    return {"id": user_id, "email": email, "password": password, "role": role}


def login(email: str, password: str = TEST_PASSWORD) -> str:
    """走登录接口拿 token（这里**必须**走接口：其它测试依赖"真发 token"这条路径）。"""
    resp = TestClient(app).post(
        "/api/v1/auth/login", json={"email": email, "password": password}
    )
    assert resp.status_code == 200, f"登录失败：{resp.status_code} {resp.text}"
    return resp.json()["access_token"]


def make_auth_client(user: dict | None = None, **kwargs) -> tuple[TestClient, dict]:
    """造一个已登录的 TestClient。

    Args:
        user: 已有的账号 dict；不传则现场建一个。
        **kwargs: 透传给 TestClient（如 `raise_server_exceptions=False`）。

    Returns:
        `(client, user)`。client 的默认请求头已带 `Authorization`。
    """
    user = user or create_user()
    token = login(user["email"], user["password"])
    client = TestClient(app, **kwargs)
    # httpx.Client.headers 是可变的：之后每个请求都自动带上，用例里不用逐条写 headers
    client.headers.update({"Authorization": f"Bearer {token}"})
    return client, user


def auth_client(**kwargs) -> TestClient:
    """:func:`make_auth_client` 的便捷形态（只取 client）。

    给"模块级 `client = TestClient(app)`"这种既有写法用一行替换即可。
    """
    client, _ = make_auth_client(**kwargs)
    return client


def purge_test_users() -> int:
    """删掉所有 `@p18.test` 账号、它们的会话，以及它们的工作区目录。

    工作区是 `<repo>/workspace/<user_id>/`，所以清账号的同时必须把目录
    一起删掉 —— 否则每跑一次测试就在磁盘上留一批带 `Agent.md` 的目录，
    越积越多（而且看起来很像"真实用户"）。
    """
    import shutil

    db = SessionLocal()
    removed = 0
    ids: list[str] = []
    try:
        users = db.query(User).filter(User.email.like(f"%@{TEST_EMAIL_DOMAIN}")).all()
        if not users:
            return 0
        ids = [u.id for u in users]
        db.query(DbSession).filter(DbSession.user_id.in_(ids)).delete(
            synchronize_session=False
        )
        # 测试会建供应商配置，必须一起清（它们在界面上是真实存在的条目）
        from app.db.models import LlmProvider

        db.query(LlmProvider).filter(LlmProvider.user_id.in_(ids)).delete(
            synchronize_session=False
        )
        # MCP 工具源同理（里面还存着加密的 headers/env，更不该留）
        from app.db.models import McpServer

        db.query(McpServer).filter(McpServer.user_id.in_(ids)).delete(
            synchronize_session=False
        )
        db.query(User).filter(User.id.in_(ids)).delete(synchronize_session=False)
        db.commit()
        removed = len(ids)
    except Exception:  # noqa: BLE001 清理失败不该让测试结果变成"失败"
        db.rollback()
        removed = 0
    finally:
        db.close()

    # 删工作区目录（用 workspace_for 拼路径，别在这里手拼字符串）
    try:
        from app.core.workspace import workspace_for

        for uid in ids:
            shutil.rmtree(workspace_for(uid), ignore_errors=True)
    except Exception:  # noqa: BLE001
        pass
    return removed


__all__ = [
    "TEST_EMAIL_DOMAIN",
    "TEST_PASSWORD",
    "auth_client",
    "create_user",
    "login",
    "make_auth_client",
    "purge_test_users",
    "unique_email",
]
