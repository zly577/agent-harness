"""pytest 全局钩子。

背景：aiosqlite 全局连接（checkpointer/store）在测试进程结束时未主动释放，
导致 Python 解释器退出挂起（每个 pytest 进程结束都 hang，表现为命令超时）。
这里在 session 结束时强制关闭该连接，并用 os._exit 兜底避免解释器资源回收挂住。

新增：认证相关 fixture（`auth_client` / `admin_client` / `anon_client`）
与测试账号清理。
"""
import asyncio
import os
import sys

import pytest


@pytest.fixture(scope="session", autouse=True)
def _close_sqlite_on_finish():
    yield
    # 见下方 _purge_test_accounts 的说明：这段 teardown 在 os._exit 路径上
    # 其实执行不到，真正生效的是 pytest_sessionfinish 里的显式调用。
    _close_sqlite_connection()


def _close_sqlite_connection() -> None:
    """关闭 langgraph 的全局 aiosqlite 连接（幂等、尽力而为）。

    不关的话解释器退出会挂住（每个 pytest 进程都 hang，表现为命令超时）。
    """
    try:
        from app.agent import context

        conn = context._conn
        if conn is not None:
            asyncio.run(conn.close())
            context._conn = None
    except Exception:
        pass


@pytest.fixture(scope="session", autouse=True)
def _purge_test_accounts():
    """（尽量）在 session 结束时清理测试账号。

    ⚠️ 实测**这个 fixture 的 teardown 不会执行** —— `pytest_sessionfinish` 里的
    `os._exit()` 跑在 session 级 fixture 收尾之前。真正生效的是
    `pytest_sessionfinish` 里对 `_purge_test_accounts()` 的显式调用。
    保留它只为"不经过 os._exit 的路径"（例如将来有人去掉 os._exit）也能收尾。
    """
    yield
    _purge_test_accounts()


def _purge_test_accounts() -> None:
    """删掉所有 `@p18.test` 测试账号（含它们的会话）。"""
    try:
        from tests.helpers import purge_test_users

        removed = purge_test_users()
        if removed:
            print(f"[conftest] 已清理 {removed} 个测试账号")
    except Exception:
        pass


# ============================================================================
# 认证 fixture
# ============================================================================


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


def _print_summary(session, exitstatus) -> None:
    """自己打一行结果汇总。

    为什么要自己打：`os._exit` 必然切断 pytest 自己的收尾输出。终端的汇总行
    属于**钩子包装器**（wrapper）的收尾部分，跑在任何 `trylast=True` 的实现
    **之后** —— 也就是说只要这里退出，那行统计就永远打不出来（实测：只剩
    `[100%]` 和 warnings，`N passed` 不见了）。失败时用户只能看到堆栈，
    得自己数挂了几个。既然拦不住顺序，就自己算一遍再打。
    """
    reporter = session.config.pluginmanager.get_plugin("terminalreporter")
    stats = getattr(reporter, "stats", None) or {}
    passed = len(stats.get("passed", ()))
    failed = len(stats.get("failed", ())) + len(stats.get("error", ()))
    skipped = len(stats.get("skipped", ()))
    total = getattr(session, "testscollected", 0)
    print(
        f"[conftest] 测试结果：{passed} passed / {failed} failed / "
        f"{skipped} skipped（收集 {total}，exitstatus={exitstatus}）"
    )


@pytest.hookimpl(trylast=True)
def pytest_sessionfinish(session, exitstatus):
    """收尾：清理测试账号 → 关 SQLite 连接 → 打印汇总。

    ## ⚠️ 这里曾经有个 `os._exit()`，现在去掉了

    历史上的问题是 aiosqlite 的全局连接没被关闭 → 解释器退出挂住 →
    每个 pytest 进程结束都 hang。当时的兜底是 `os._exit(exitstatus)`。

      发现这个兜底有两个真实代价：
    1. 它会**跳过 session 级 fixture 的 teardown**（清理逻辑不执行）；
    2. 它会**吞掉 pytest 的 FAILURES 段落**（失败明细和统计都在收尾输出里）——
       失败时只看到一行 `197 passed / 3 failed`，完全不知道是哪 3 个、
       为什么失败。这比"退出慢 3 秒"糟糕得多。

    而它要解决的那个问题已经被 `_close_sqlite_connection()` 正面修掉了
    （关掉连接本来就是正确做法，不依赖强杀）。所以这里改成只做清理，
    把进程退出交回给 pytest。
    """
    _purge_test_accounts()
    _close_sqlite_connection()
    _print_summary(session, exitstatus)
