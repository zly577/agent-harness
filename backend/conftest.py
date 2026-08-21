"""pytest 全局钩子。

背景：P2 的 aiosqlite 全局连接（checkpointer/store）在测试进程结束时未主动释放，
导致 Python 解释器退出挂起（每个 pytest 进程结束都 hang，表现为命令超时）。
这里在 session 结束时强制关闭该连接，并用 os._exit 兜底避免解释器资源回收挂住。
"""
import asyncio
import os

import pytest


@pytest.fixture(scope="session", autouse=True)
def _close_sqlite_on_finish():
    yield
    # session 结束后关闭 aiosqlite 连接，避免解释器退出阻塞
    try:
        from app.agent import context
        conn = context._conn
        if conn is not None:
            asyncio.run(conn.close())
            context._conn = None
    except Exception:
        pass


def pytest_sessionfinish(session, exitstatus):
    # 所有测试已完成；强制退出以跳过 aiosqlite 等后台线程导致的解释器退出 hang。
    # NOTE: 结果与退出码已在此时确定。
    os._exit(exitstatus)
