"""P10+ 测试：工具调用流水持久化（tool_call_logs）与历史合并。

覆盖：
- _log_tool_event 落库（call 插入 running / result 回填 done）
- _load_tool_log 读取顺序与字段
- _merge_tool_log：补齐缺失结果 / 兜底重建 / 无日志时原样返回
"""
import uuid

from app.agent.runtime import _load_tool_log, _log_tool_event, _merge_tool_log
from app.db.base import Base
from app.db.models import Session as DbSession  # noqa: F401  确保模型全部注册
from app.db.models import ToolCallLog
from app.db.session import SessionLocal, engine

# 测试进程不走应用 lifespan，这里幂等建表（生产由 main.py 启动时创建）
Base.metadata.create_all(bind=engine)


def _cleanup(sid: str) -> None:
    db = SessionLocal()
    try:
        db.query(ToolCallLog).filter(ToolCallLog.session_id == sid).delete()
        db.commit()
    finally:
        db.close()


def test_tool_log_roundtrip():
    sid = uuid.uuid4().hex
    try:
        _log_tool_event(sid, "call", "call_1", "ls", args={"path": "/"})
        _log_tool_event(sid, "call", "call_2", "task", args={"subagent_type": "researcher"})
        _log_tool_event(sid, "result", "call_1", "ls", result="a.md\nb.md")

        rows = _load_tool_log(sid)
        assert [r["call_id"] if "call_id" in r else r["id"] for r in rows] == ["call_1", "call_2"]
        first, second = rows
        assert first["name"] == "ls" and first["kind"] == "tool"
        assert first["status"] == "done" and first["result"] == "a.md\nb.md"
        assert first["args"] == {"path": "/"}
        # task 委派自动归类 kind=task，结果未回填保持 running
        assert second["kind"] == "task" and second["status"] == "running"

        _log_tool_event(sid, "result", "call_2", "task", result="报告内容")
        rows = _load_tool_log(sid)
        assert rows[1]["status"] == "done" and rows[1]["result"] == "报告内容"
    finally:
        _cleanup(sid)


def test_result_before_call_still_persisted():
    """结果先于调用记录到达（异常时序）也应落一条完整记录。"""
    sid = uuid.uuid4().hex
    try:
        _log_tool_event(sid, "result", "call_x", "ls", result="orphan")
        rows = _load_tool_log(sid)
        assert len(rows) == 1
        assert rows[0]["id"] == "call_x" and rows[0]["result"] == "orphan"
        assert rows[0]["status"] == "done"
    finally:
        _cleanup(sid)


def test_merge_fills_missing_result():
    """回放出的调用结果为空（如中途被关闭）→ 用日志补齐。"""
    history = [
        {"role": "user", "content": "hi"},
        {"role": "assistant", "content": "", "toolCalls": [
            {"id": "call_1", "name": "ls", "args": {"path": "/"}, "result": "", "status": "done"},
        ], "delegations": []},
    ]
    log_rows = [
        {"id": "call_1", "name": "ls", "kind": "tool", "args": {"path": "/"},
         "result": "a.md", "status": "done"},
    ]
    merged = _merge_tool_log(history, log_rows)
    assert merged[1]["toolCalls"][0]["result"] == "a.md"


def test_merge_rebuilds_when_history_has_no_tools():
    """checkpoint 被压缩裁剪后回放无任何工具痕迹 → 用日志兜底重建。"""
    history = [
        {"role": "user", "content": "hi"},
        {"role": "assistant", "content": "已处理", "toolCalls": [], "delegations": []},
    ]
    log_rows = [
        {"id": "call_1", "name": "ls", "kind": "tool", "args": {"path": "/"},
         "result": "a.md", "status": "done"},
        {"id": "call_2", "name": "task", "kind": "task",
         "args": {"subagent_type": "researcher", "description": "调研"},
         "result": "报告", "status": "done"},
    ]
    merged = _merge_tool_log(history, log_rows)
    assert len(merged) == 3
    extra = merged[-1]
    assert extra["role"] == "assistant"
    assert len(extra["toolCalls"]) == 1 and len(extra["delegations"]) == 1
    assert extra["delegations"][0]["subagent_type"] == "researcher"


def test_merge_noop_without_log_rows():
    history = [{"role": "assistant", "content": "x", "toolCalls": [], "delegations": []}]
    merged = _merge_tool_log(history, [])
    assert merged is history and len(merged) == 1


def test_merge_keeps_existing_results():
    """回放已有结果时不被日志覆盖。"""
    history = [
        {"role": "assistant", "content": "", "toolCalls": [
            {"id": "call_1", "name": "ls", "args": {}, "result": "原始结果", "status": "done"},
        ], "delegations": []},
    ]
    log_rows = [
        {"id": "call_1", "name": "ls", "kind": "tool", "args": {},
         "result": "日志结果", "status": "done"},
    ]
    merged = _merge_tool_log(history, log_rows)
    assert merged[0]["toolCalls"][0]["result"] == "原始结果"
