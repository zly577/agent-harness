"""Isolated test storage and authentication fixtures.

Tests never use the application database, workspace, or provider credentials.
Use HARNESS_TEST_MYSQL_URL only for a dedicated database whose name ends in _test.
"""
import asyncio
import base64
import os
import tempfile
from pathlib import Path

import pytest

_TEST_DIRECTORY = tempfile.TemporaryDirectory(prefix="agent-harness-tests-")
_TEST_ROOT = Path(_TEST_DIRECTORY.name)
_TEST_MYSQL = os.environ.get("HARNESS_TEST_MYSQL_URL", "")
if _TEST_MYSQL:
    from sqlalchemy.engine import make_url
    if not (make_url(_TEST_MYSQL).database or "").endswith("_test"):
        raise pytest.UsageError("HARNESS_TEST_MYSQL_URL must name a dedicated database ending in _test")

# Set these before importing application modules (some tests use them at collection).
os.environ.update({
    "MYSQL_URL": _TEST_MYSQL or "sqlite:///" + (_TEST_ROOT / "business.sqlite").as_posix(),
    "SQLITE_PATH": str(_TEST_ROOT / "checkpoint.sqlite"),
    "HARNESS_WORKSPACE": str(_TEST_ROOT / "workspace"),
    "APP_ENV": "development",
    "JWT_SECRET": "isolated-tests-only-secret-00000000",
    "FERNET_KEY": base64.urlsafe_b64encode(os.urandom(32)).decode(),
    "DEEPSEEK_API_KEY": "",
    "BOOTSTRAP_ADMIN_EMAIL": "",
    "BOOTSTRAP_ADMIN_PASSWORD": "",
    "SMTP_HOST": "",
    "SMTP_PASSWORD": "",
    "SMTP_OAUTH_CLIENT_SECRET": "",
    "SMTP_OAUTH_REFRESH_TOKEN": "",
    "MCP_ENABLED": "false",
    "SANDBOX_ENABLED": "false",
    "HARNESS_WINDOWS_SHELL": "auto",
})


def pytest_configure(config):
    if not config.option.basetemp:
        config.option.basetemp = str(_TEST_ROOT / "pytest")
    from app.db.base import Base
    from app.db import models  # noqa: F401
    from app.db.session import engine
    Base.metadata.create_all(engine)


@pytest.fixture(scope="session", autouse=True)
def application_resources():
    yield
    from tests.helpers import purge_test_users
    from app.agent import context
    from app.db.session import engine
    try:
        purge_test_users()
    finally:
        asyncio.run(context.aclose())
        engine.dispose()
        _TEST_DIRECTORY.cleanup()


@pytest.fixture(autouse=True)
def no_external_http(monkeypatch):
    """Real providers must be explicitly mocked; TestClient/MockTransport work."""
    import httpx

    def blocked(*args, **kwargs):
        raise AssertionError("External HTTP is disabled in tests; use httpx.MockTransport")

    async def blocked_async(*args, **kwargs):
        blocked()

    monkeypatch.setattr(httpx.HTTPTransport, "handle_request", blocked)
    monkeypatch.setattr(httpx.AsyncHTTPTransport, "handle_async_request", blocked_async)


@pytest.fixture(scope="session")
def auth_user():
    """一个普通用户账号（整个 session 复用，避免每个用例都建账号）。"""
    from tests.helpers import create_user

    return create_user(display_name="普通测试用户")


@pytest.fixture(scope="session")
def admin_user():
    """一个管理员账号（给"全局开关 / 系统级资源"这类 admin 专属用例用）。"""
    from tests.helpers import create_user, unique_email

    return create_user(email=unique_email("admin"), display_name="测试管理员", role="admin")


@pytest.fixture(scope="session")
def auth_token(auth_user):
    """普通用户的 access token。"""
    from tests.helpers import login

    return login(auth_user["email"], auth_user["password"])


@pytest.fixture
def auth_client(auth_token):
    """已登录的 TestClient（普通用户）。

    用 `raise_server_exceptions=False`：部分 API 会把异常转成 HTTP 状态码，
    让 500 以响应形式返回、由用例断言更好定位（与 `test_files_api.py` 既有写法一致）。
    """
    from fastapi.testclient import TestClient

    from app.main import app

    client = TestClient(app, raise_server_exceptions=False)
    client.headers.update({"Authorization": f"Bearer {auth_token}"})
    return client


@pytest.fixture
def admin_client(admin_user):
    """已登录的 TestClient（管理员）。"""
    from fastapi.testclient import TestClient

    from app.main import app
    from tests.helpers import login

    token = login(admin_user["email"], admin_user["password"])
    client = TestClient(app, raise_server_exceptions=False)
    client.headers.update({"Authorization": f"Bearer {token}"})
    return client


@pytest.fixture
def anon_client():
    """未登录的 TestClient（验 401 用）。"""
    from fastapi.testclient import TestClient

    from app.main import app

    return TestClient(app, raise_server_exceptions=False)
