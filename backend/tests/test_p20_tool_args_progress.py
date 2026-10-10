"""P20 补充测试：工具参数生成期的进度心跳（`tool_args` 事件）。

## 要解决的**真实事故**（2026-09-30，会话 `67abe4a6…`）

模型把大段内容塞进**工具参数**（不是正文 `content`）时，`tool_call` 事件必须等参数 JSON
**拼完整**才发得出来。实测那次 `write_file` 的 `content` 参数 ≈ **82KB / 468 秒**：

- `AIMessage.content` 是**空串** ⇒ 没有任何 token 增量；
- 参数没拼完 ⇒ `tool_call` / `tool_result` 都发不出来；
- `logs/backend.log` 这 8 分钟**一行不出**，`psutil` 看进程 CPU≈0（Windows 下 io_counters
  还不含 socket，更容易误判成"没在传数据"）。

于是前端表现为"卡死了、没有后文也没有完成"，而进程其实以 **≈69 token/s 全速输出**。

修复 = 参数拼装期间按**两段节流**发一条心跳（`tool_args`），把"它还在写"暴露出来。

⚠️ 边界：心跳只能在**有 chunk 到达**时发出，所以它覆盖不了"对端静默挂起"
（那种情况靠 `settings.llm_stream_chunk_timeout_seconds` 兜底）。

## 覆盖

| 行为 | 用例 |
|------|------|
| 两段节流（先 after、再 interval） | `test_progress_waits_after_threshold`、`test_progress_throttled_by_interval` |
| 开关关闭即静默 | `test_progress_disabled` |
| 体积按 UTF-8 字节（中文不被低估） | `test_progress_uses_utf8_bytes` |
| 非流式兜底路径没有 first_ts 时静默 | `test_progress_without_first_ts_is_silent` |
| **真图真链路**：分片 → 心跳 → 正式 tool_call | `test_agent_event_stream_emits_progress_then_tool_call` |
| SSE 契约映射 | `test_to_sse_event_maps_tool_args` |
| 回放缓冲只留最新一条（且不做字符串拼接） | `test_run_buffer_keeps_only_latest_progress` |
"""
from __future__ import annotations

import asyncio
import json
import uuid
from contextlib import contextmanager

from langchain.agents import create_agent
from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.messages import AIMessage, AIMessageChunk
from langchain_core.outputs import ChatGeneration, ChatGenerationChunk, ChatResult
from langchain_core.tools import tool
from langgraph.checkpoint.memory import MemorySaver

from app.agent import runtime
from app.agent.runner import RunManager, SessionRun, to_sse_event
from app.agent.runtime import args_progress_event
from app.core.config import settings
from app.db.base import Base
from app.db.models import Session as DbSession  # noqa: F401  确保模型全部注册
from app.db.models import TaskStep, ToolCallLog
from app.db.session import SessionLocal, engine

# 测试进程不走应用 lifespan，这里幂等建表（生产由迁移 + lifespan 负责）
Base.metadata.create_all(bind=engine)


# ============================================================================
# 假模型：把 tool_call 参数**按分片**流式吐出来
# ============================================================================


class _StreamingArgsModel(BaseChatModel):
    """按脚本分片吐 `tool_call_chunks` 的模型（模拟"大参数慢慢来"）。

    为什么必须实现 `_astream`：langchain 的 `_should_stream` 只要发现子类实现了
    `_astream` 就走流式路径，`tool_call_chunks` 才会出现在事件流里 —— 而真实事故
    （82KB 参数、468 秒静默）正好发生在**这条路径**上。只实现 `_generate` 的话会走
    "一次性给出完整 tool_calls"的兜底分支，心跳压根不会被触发，测了个寂寞。

    scripts 每项两种形状：
      - `{"name","id","args_pieces":[...]}`：分片流出一次工具调用（最后一片才补成合法 JSON）
      - `{"text": "..."}`：普通文字回复（收尾用）
    """

    scripts: list
    cursor: int = 0

    @property
    def _llm_type(self) -> str:
        return "streaming-args"

    def bind_tools(self, tools, **kwargs):  # noqa: ANN001, ANN003
        return self

    def _take(self) -> dict:
        idx = min(self.cursor, len(self.scripts) - 1)
        self.cursor += 1
        return self.scripts[idx]

    def _generate(self, messages, stop=None, run_manager=None, **kwargs):  # noqa: ANN001
        """兜底：万一没走流式，也要能把图跑完（把分片拼成完整 AIMessage）。"""
        s = self._take()
        if "args_pieces" in s:
            full = json.loads("".join(s["args_pieces"]))
            msg = AIMessage(content="", tool_calls=[
                {"name": s["name"], "args": full, "id": s["id"], "type": "tool_call"},
            ])
        else:
            msg = AIMessage(content=s.get("text", ""))
        return ChatResult(generations=[ChatGeneration(message=msg)])

    async def _astream(self, messages, stop=None, run_manager=None, **kwargs):  # noqa: ANN001
        s = self._take()
        if "args_pieces" not in s:
            yield ChatGenerationChunk(message=AIMessageChunk(content=s.get("text", "")))
            return
        for i, piece in enumerate(s["args_pieces"]):
            yield ChatGenerationChunk(message=AIMessageChunk(
                content="",
                tool_call_chunks=[{
                    # name / id 只在首块给（真实的 OpenAI 流就是这样）
                    "name": s["name"] if i == 0 else None,
                    "args": piece,
                    "id": s["id"] if i == 0 else None,
                    "index": 0,
                    "type": "tool_call_chunk",
                }],
            ))


@tool("probe_write")
def probe_write(file_path: str, content: str = "") -> str:
    """探针：记录收到的参数体积（断言"工具最终拿到的是完整参数"）。"""
    return f"wrote:{file_path}:{len(content)}"


#: 参数切 4 片，前 3 片都是**非法 JSON** ⇒ 每次到达都该触发一条心跳
_ARGS_PIECES = [
    '{"file_path": "/workspace/big.md", "content": "',
    "甲" * 400,
    "乙" * 400,
    '"}',
]


def _progress_script() -> list:
    return [
        {"name": "probe_write", "id": "call_big", "args_pieces": list(_ARGS_PIECES)},
        {"text": "写好了"},
    ]


def _cleanup_session(session_id: str) -> None:
    db = SessionLocal()
    try:
        db.query(ToolCallLog).filter(ToolCallLog.session_id == session_id).delete()
        db.query(TaskStep).filter(TaskStep.session_id == session_id).delete()
        db.commit()
    finally:
        db.close()


@contextmanager
def _patch_agent(agent):
    """把 `runtime.get_agent` 临时换成已构造好的图（不发真实模型请求）。"""
    original = runtime.get_agent

    async def _patched(*args, **kwargs):  # noqa: ANN002, ANN003
        return agent

    runtime.get_agent = _patched
    try:
        yield agent
    finally:
        runtime.get_agent = original


def _run_events(agent, session_id: str, user_id: str) -> list[tuple[str, dict]]:
    with _patch_agent(agent):
        async def _run():
            out = []
            async for kind, data in runtime.agent_event_stream(
                session_id, {"messages": [{"role": "user", "content": "开始"}]},
                user_id=user_id,
            ):
                out.append((kind, data))
            return out

        return asyncio.run(_run())


def _entry(**kw) -> dict:
    """`pending` 里那条记录的等价物（字段与 `agent_event_stream` 里创建的一致）。"""
    e = {"id": "c1", "name": "write_file", "args_str": "", "first_ts": 100.0, "last_emit": 0.0}
    e.update(kw)
    return e


# ============================================================================
# 第 1 层：节流判断（纯函数，注入 now ⇒ 完全确定）
# ============================================================================


def test_progress_disabled(monkeypatch):
    monkeypatch.setattr(settings, "tool_args_progress_enabled", False)
    assert args_progress_event(_entry(args_str="x" * 100), 200.0) is None


def test_progress_waits_after_threshold(monkeypatch):
    """① 先等 after 秒：正常的小工具调用几毫秒就跑完，不该被打扰。"""
    monkeypatch.setattr(settings, "tool_args_progress_after_seconds", 3.0)
    monkeypatch.setattr(settings, "tool_args_progress_interval_seconds", 2.0)
    e = _entry(args_str="x" * 10)
    assert args_progress_event(e, 100.0) is None       # 刚开始
    assert args_progress_event(e, 102.9) is None       # 差 0.1 秒
    assert args_progress_event(e, 103.0) is not None   # 到点发第一条


def test_progress_throttled_by_interval(monkeypatch):
    """② 之后每 interval 秒一条 —— 8 分钟最多约 240 条。"""
    monkeypatch.setattr(settings, "tool_args_progress_after_seconds", 1.0)
    monkeypatch.setattr(settings, "tool_args_progress_interval_seconds", 2.0)
    e = _entry(args_str="x" * 10)
    assert args_progress_event(e, 110.0) is not None   # 第一条
    assert args_progress_event(e, 111.9) is None       # 不到点
    assert args_progress_event(e, 112.0) is not None   # 到点
    assert args_progress_event(e, 113.0) is None       # 又不到点
    # 每次都刷新 last_emit，所以是"间隔"而不是"累计"节流
    assert e["last_emit"] == 112.0


def test_progress_uses_utf8_bytes(monkeypatch):
    """参数里大量是中文：kb 必须按 UTF-8 字节算，否则体积被低估 3 倍。"""
    monkeypatch.setattr(settings, "tool_args_progress_after_seconds", 0.0)
    monkeypatch.setattr(settings, "tool_args_progress_interval_seconds", 0.0)
    got = args_progress_event(_entry(args_str="中" * 1000), 200.0)
    assert got is not None
    assert got["chars"] == 1000
    assert got["kb"] == round(3000 / 1024, 1)
    assert got["id"] == "c1" and got["name"] == "write_file"
    assert got["elapsed"] == 100.0
    # 心跳**不能**带参数正文：那次参数有 82KB，推到前端纯属浪费
    assert "content" not in got and "args" not in got


def test_progress_without_first_ts_is_silent():
    """非流式兜底路径建的 entry 没有 first_ts ⇒ 静默（防御性）。"""
    assert args_progress_event({"args_str": "x" * 10}, 1e9) is None


# ============================================================================
# 第 2 层：真图真链路（分片 → 心跳 → tool_call → 工具收到完整参数）
# ============================================================================


def test_agent_event_stream_emits_progress_then_tool_call(monkeypatch):
    """4 片参数、前 3 片非法 JSON ⇒ 3 条心跳，第 4 片才出 `tool_call`。"""
    # 把两段节流都关掉：真实时间是微秒级，不这样做一条心跳都不会发
    monkeypatch.setattr(settings, "tool_args_progress_after_seconds", 0.0)
    monkeypatch.setattr(settings, "tool_args_progress_interval_seconds", 0.0)

    sid = uuid.uuid4().hex
    uid = "u-" + uuid.uuid4().hex[:8]
    agent = create_agent(
        model=_StreamingArgsModel(scripts=_progress_script()),
        tools=[probe_write],
        system_prompt="（测试用系统提示）",
        checkpointer=MemorySaver(),
    )
    try:
        events = _run_events(agent, sid, uid)
    finally:
        _cleanup_session(sid)

    kinds = [k for k, _ in events]
    progress = [d for k, d in events if k == "tool_args"]
    calls = [d for k, d in events if k == "tool_call"]

    # 心跳真的发出了，而且**在** tool_call 之前（否则用户还是要干等）
    assert len(progress) == len(_ARGS_PIECES) - 1, progress
    assert "tool_args" in kinds and "tool_call" in kinds
    assert max(i for i, k in enumerate(kinds) if k == "tool_args") \
        < kinds.index("tool_call")

    # 体积单调增长，且始终 < 完整参数（拼完那一刻就该出 tool_call 了）
    full_chars = len("".join(_ARGS_PIECES))
    chars = [p["chars"] for p in progress]
    assert chars == sorted(chars)
    assert all(0 < c < full_chars for c in chars)
    assert progress[0]["name"] == "probe_write"
    assert progress[0]["id"] == "call_big"
    assert progress[-1]["kb"] > 0
    assert all(p["elapsed"] >= 0 for p in progress)

    # 心跳不影响正式链路：tool_call 参数完整、工具真的被调用、参数对
    assert len(calls) == 1
    assert calls[0]["args"]["content"] == "甲" * 400 + "乙" * 400
    results = [d for k, d in events if k == "tool_result"]
    assert results and results[0]["content"].endswith(":800")


def test_progress_disabled_means_no_events(monkeypatch):
    """开关关掉 → 这条链路上一个 `tool_args` 都不该有（可回退到旧行为）。"""
    monkeypatch.setattr(settings, "tool_args_progress_enabled", False)

    sid = uuid.uuid4().hex
    uid = "u-" + uuid.uuid4().hex[:8]
    agent = create_agent(
        model=_StreamingArgsModel(scripts=_progress_script()),
        tools=[probe_write],
        system_prompt="（测试用系统提示）",
        checkpointer=MemorySaver(),
    )
    try:
        events = _run_events(agent, sid, uid)
    finally:
        _cleanup_session(sid)

    assert [k for k, _ in events].count("tool_args") == 0
    # 但正式链路照旧
    assert [k for k, _ in events].count("tool_call") == 1


# ============================================================================
# 第 3 层：SSE 契约与回放缓冲
# ============================================================================


def test_to_sse_event_maps_tool_args():
    evt = to_sse_event("tool_args", {
        "id": "c1", "name": "write_file", "chars": 1024, "kb": 1.5, "elapsed": 4.2,
    })
    assert evt["type"] == "tool_args"
    assert evt["id"] == "c1" and evt["name"] == "write_file"
    assert evt["kb"] == 1.5 and evt["elapsed"] == 4.2


def test_run_buffer_keeps_only_latest_progress():
    """心跳是"最新有效"语义：同一个工具只留在最后一条，且**不做字符串拼接**。

    如果把它塞进 `_DELTA_FIELDS`，`kb` 会被当字符串拼成 "1.09.0" —— 数字语义直接废掉。
    """
    run = SessionRun(session_id="s1")
    RunManager._buffer(run, {"type": "tool_args", "id": "c1", "kb": 1.0})
    RunManager._buffer(run, {"type": "tool_args", "id": "c1", "kb": 9.0})
    assert len(run.events) == 1
    assert run.events[0]["kb"] == 9.0

    # 不同工具各自成条
    RunManager._buffer(run, {"type": "tool_args", "id": "c2", "kb": 2.0})
    assert [e["id"] for e in run.events] == ["c1", "c2"]

    # 中间夹了别的事件就不再合并（只合并紧邻的同 id 心跳），避免回放顺序错乱
    RunManager._buffer(run, {"type": "token", "content": "x"})
    RunManager._buffer(run, {"type": "tool_args", "id": "c2", "kb": 3.0})
    assert [e["type"] for e in run.events] == ["tool_args", "tool_args", "token", "tool_args"]
    assert run.events[-1]["kb"] == 3.0
