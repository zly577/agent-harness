"""P2 测试：存储层（MySQL 会话 + SQLite checkpoint），不触发真实模型调用。

P18：`create_session()` 现在**必须**带 user_id（否则会写出无主的会话行 ——
那对谁都不可见，等于每次跑测试都往库里扔一条垃圾）。这里用一个 `@p18.test`
测试账号，跑完由 `conftest._purge_test_accounts` 连同它的会话一起删掉。
"""
import sqlite3

import pytest


def _test_user() -> dict:
    """拿一个测试账号（复用 helpers，收尾会被自动清理）。"""
    from tests.helpers import create_user, unique_email

    return create_user(email=unique_email("p2"), display_name="P2 测试")


def _create_test_session() -> str:
    """通过 runtime 建一个会话（真实写入 MySQL），返回 session_id。"""
    from app.agent.runtime import create_session, session_exists

    user = _test_user()
    sid = create_session(user["id"])
    assert sid and len(sid) == 32
    assert session_exists(sid) is True
    return sid


def test_create_and_check_session():
    _create_test_session()
    # 查不到不存在的会话
    from app.agent.runtime import session_exists
    assert session_exists("0000000") is False


def test_session_row_in_mysql():
    from app.agent.runtime import create_session
    from app.db.models import Session as DbSession
    from app.db.session import SessionLocal

    user = _test_user()
    sid = create_session(user["id"])
    db = SessionLocal()
    try:
        row = db.get(DbSession, sid)
        assert row is not None
        assert row.status == "active"
        assert row.title == "新会话"
        # P18：归属必须落库，否则这个会话谁都看不到
        assert row.user_id == user["id"]
    finally:
        db.close()


def test_sqlite_checkpoint_metadata():
    """确认 SQLite 会话历史表确实会落盘（进程重启可用）。"""
    import asyncio

    from app.agent.context import get_checkpointer
    # 只验证能构建 & 建表，不真的发消息
    async def _probe():
        cp = await get_checkpointer()
        return cp
    asyncio.run(_probe())
    conn = sqlite3.connect("data/harness.sqlite")
    try:
        tables = {r[0] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        )}
        assert "checkpoints" in tables
        assert "writes" in tables
        assert "store" in tables
    finally:
        conn.close()


def test_sqlite_checkpoint_metadata():
    """确认 SQLite 会话历史表确实会落盘（进程重启可用）。"""
    import asyncio

    from app.agent.context import get_checkpointer
    # 只验证能构建 & 建表，不真的发消息
    async def _probe():
        cp = await get_checkpointer()
        return cp
    asyncio.run(_probe())
    conn = sqlite3.connect("data/harness.sqlite")
    try:
        tables = {r[0] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        )}
        assert "checkpoints" in tables
        assert "writes" in tables
        assert "store" in tables
    finally:
        conn.close()
