"""P2 测试：存储层（MySQL 会话 + SQLite checkpoint），不触发真实模型调用。"""
import sqlite3

import pytest


def _create_test_session():
    """通过 runtime 建一个会话（真实写入 MySQL），返回 session_id。"""
    from app.agent.runtime import create_session, session_exists
    sid = create_session()
    assert sid and len(sid) == 32
    assert session_exists(sid) is True
    return sid


def test_create_and_check_session():
    sid = _create_test_session()
    # 查不到 gga 会话
    from app.agent.runtime import session_exists
    assert session_exists("0000000") is False


def test_session_row_in_mysql():
    sid = _create_test_session()
    from app.db.models import Session as DbSession
    from app.db.session import SessionLocal
    db = SessionLocal()
    try:
        row = db.get(DbSession, sid)
        assert row is not None
        assert row.status == "active"
        assert row.title == "新会话"
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
