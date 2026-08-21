"""P5 测试：中间件框架（不触发真实模型调用）。"""
import sqlite3

import pytest

from app.agent.middleware import (
    AuditMiddleware,
    LoggingMiddleware,
    RateLimitMiddleware,
    _safe_messages,
)


def test_middleware_instantiable():
    """三个中间件都能实例化并具有预期 name。"""
    assert LoggingMiddleware().name == "harness-logging"
    assert AuditMiddleware().name == "harness-audit"
    assert RateLimitMiddleware(max_messages=0).name == "harness-rate-limit"


def test_safe_messages_handles_none():
    assert _safe_messages(None) == []
    assert _safe_messages({}) == []
    assert _safe_messages({"messages": ["a", "b"]}) == ["a", "b"]


def test_rate_limit_instantiates_with_max():
    rl = RateLimitMiddleware(max_messages=5)
    assert rl._max == 5


def test_rate_limit_lower_than_0_is_disabled():
    rl = RateLimitMiddleware(max_messages=0)
    assert rl._max == 0


def test_audit_log_model_columns():
    """audit_logs 模型字段正确。"""
    from app.db.models import AuditLog
    cols = [c.name for c in AuditLog.__table__.columns]
    assert {"id", "thread_id", "run_id", "action", "detail", "created_at"} <= set(cols)


def test_audit_write_to_mysql():
    """_write_audit 能把一条记录写入 MySQL（真实审计入库）。"""
    import asyncio
    from sqlalchemy import select

    async def _run():
        from app.agent.middleware import _write_audit
        await _write_audit(
            thread_id="atest-thread",
            run_id="atest-run",
            action="test_event",
            detail={"k": "v"},
        )
    asyncio.run(_run())

    from app.db.session import SessionLocal
    from app.db.models import AuditLog
    db = SessionLocal()
    try:
        row = db.execute(
            select(AuditLog).where(AuditLog.thread_id == "atest-thread").limit(1)
        ).scalar_one_or_none()
        assert row is not None
        assert row.action == "test_event"
        assert '{"k": "v"}' in (row.detail or "")
        # 清理测试记录，避免污染
        db.delete(row)
        db.commit()
    finally:
        db.close()
