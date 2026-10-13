"""测试：工具调用意图标签（`_intent` → `intent_label` / `intent_source`）。

覆盖范围（对应任务里的 6 条验收标准）：

| 验收 | 用例 |
|------|------|
| ① ≥2 次工具调用都带非空 intent_label | `test_agent_event_stream_labels_every_tool_call` |
| ② 长度 ≤24 / 无换行 / 不等于工具名 | `test_normalize_rejects_invalid`、上面那条例里的契约断言 |
| ③ 假工具收到的参数里不含 `_intent` | `test_intent_middleware_strips_before_handler`、`test_agent_event_stream_*` |
| ④ `_intent` 空/超长/JSON 仍能执行且走兜底 | `test_invalid_intent_still_runs_tool` |
| ⑤ 重放事件标签不变 | `test_replay_is_deterministic` |
| ⑥ 回填脚本连跑两次第二次 0 改动 | `test_backfill_is_idempotent` |

设计说明：`test_agent_event_stream_*` / `test_invalid_intent_still_runs_tool` 跑的是
**真实的 langgraph 图 + 真实中间件 + 真实事件抽取 + 真实落库与投影**，只把"模型"
换成一个按脚本吐 AIMessage 的假模型（`_ScriptedChatModel`）—— 这样"两个工具调用、
其中一个没给 `_intent`"这类边界是完全确定的，不会因为真实模型发挥失常变成 flaky。
真实模型那一侧由 `_probe/probe_intent_e2e.py` 验证（会真的打一次 DeepSeek）。
"""
from __future__ import annotations

import asyncio
import json
import re
import uuid
from contextlib import contextmanager
from pathlib import Path

import pytest
from langchain.agents import create_agent
from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.messages import AIMessage, ToolMessage
from langchain_core.outputs import ChatGeneration, ChatResult
from langchain_core.tools import StructuredTool, tool
from langgraph.checkpoint.memory import MemorySaver
from langgraph.prebuilt.tool_node import ToolCallRequest

from app.agent import runtime
from app.agent.intent import (
    INTENT_FIELD,
    INTENT_MAX_CHARS,
    SOURCE_BACKFILL,
    SOURCE_FALLBACK,
    SOURCE_MODEL,
    TOOL_CATALOG,
    catalog_entry,
    deterministic_label,
    fallback_label,
    normalize_intent,
    normalize_locale,
    render_intent_prompt_section,
    resolve_intent,
    strip_intent,
)
from app.agent.middleware import IntentMiddleware, augment_tool_schema
from app.db.base import Base
from app.db.models import Session as DbSession  # noqa: F401  确保模型全部注册
from app.db.models import TaskStep, ToolCallLog
from app.db.projections import project_session, project_tool_call
from app.db.session import SessionLocal, engine

# 测试进程不走应用 lifespan，这里幂等建表（生产由迁移 + lifespan 负责）
Base.metadata.create_all(bind=engine)


# ============================================================================
# 工具与脚本化模型
# ============================================================================


@tool("probe_echo")
def probe_echo(value: str) -> str:
    """把收到的参数原样返回（验收专用：用来观察"工具到底收到了什么"）。"""
    return f"echo:{value}"


@tool("probe_read")
def probe_read(file_path: str) -> str:
    """读取一个文件（假实现，只是为了让工具名落在目录里）。"""
    return f"content-of:{file_path}"


class _ScriptedChatModel(BaseChatModel):
    """按脚本依次返回预设 AIMessage 的模型（不发网络请求）。

    `bind_tools` 直接返回 self：意图标签的 schema 注入发生在
    `ModelRequest` 层面（由 `IntentMiddleware` 改写 tools 列表），与具体模型无关 ——
    这里只要"能绑定"就够了。
    """

    responses: list
    cursor: int = 0

    @property
    def _llm_type(self) -> str:
        return "scripted"

    def bind_tools(self, tools, **kwargs):  # noqa: ANN001, ANN003
        return self

    def _generate(self, messages, stop=None, run_manager=None, **kwargs):  # noqa: ANN001
        idx = min(self.cursor, len(self.responses) - 1)
        self.cursor += 1
        return ChatResult(generations=[ChatGeneration(message=self.responses[idx])])


def _make_agent(script: list, *, tools=None):
    """造一个真实 langgraph 图（create_agent + 意图中间件 + MemorySaver）。"""
    return create_agent(
        model=_ScriptedChatModel(responses=script),
        tools=list(tools) if tools is not None else [probe_echo, probe_read],
        system_prompt="（测试用系统提示）",
        middleware=[IntentMiddleware()],
        checkpointer=MemorySaver(),
    )


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
    """把 `runtime.get_agent` 临时换成某个已构造好的图。

    为什么值得这么做：事件抽取（`tool_call` / `tool_result` / `intent_label` 的
    位置）和落库/投影都在 `agent_event_stream` 里，而它唯一的外部依赖就是
    "拿一个 agent"。换掉这一个入口，就能在**不发真实模型请求**的前提下跑通整条
    链路；真实模型那条路由 `_probe/probe_intent_e2e.py` 覆盖。
    """
    original = runtime.get_agent

    async def _patched(*args, **kwargs):  # noqa: ANN002, ANN003
        return agent

    runtime.get_agent = _patched
    try:
        yield agent
    finally:
        runtime.get_agent = original


def _run_events(agent, session_id: str, user_id: str, message: str = "开始"):
    """把 `get_agent` 换成本用例的脚本化图，跑完一条真实事件流。

    Returns: `[(kind, data)]`
    """
    with _patch_agent(agent):
        async def _run():
            events = []
            async for kind, data in runtime.agent_event_stream(
                session_id, {"messages": [{"role": "user", "content": message}]},
                user_id=user_id,
            ):
                events.append((kind, data))
            return events

        return asyncio.run(_run())


def _tool_calls(events) -> list[dict]:
    return [d for k, d in events if k == "tool_call"]


def _two_call_script() -> list:
    """一次运行里两个工具调用：第一个给了 `_intent`，第二个故意不给。"""
    return [
        AIMessage(content="", tool_calls=[
            {"name": "probe_echo", "args": {"value": "a", INTENT_FIELD: "读取 a 文件"},
             "id": "call_a", "type": "tool_call"},
            {"name": "probe_echo", "args": {"value": "b"},
             "id": "call_b", "type": "tool_call"},
        ]),
        AIMessage(content="两个都完成了"),
    ]


# ============================================================================
# 第 3 层：规范化 / 兜底 / 目录
# ============================================================================


def test_normalize_accepts_good_labels():
    assert normalize_intent("查询北京今天的天气", "web_search") == "查询北京今天的天气"
    # 内部多余空白收敛成单空格（仍然单行）
    assert normalize_intent("  读取 config.py   的前 50 行 ", "read_file") == \
        "读取 config.py 的前 50 行"
    # 恰好等于上限 → 放行
    exact = "一" * INTENT_MAX_CHARS
    assert normalize_intent(exact, "ls") == exact


def test_spec_good_example_exceeds_the_cap():
    """⚠️ 记录一处**契约自相矛盾**（不修，只钉住行为）。

    任务书写明的正例 `读取 user_profile.py 的前 50 行` 是 **26 字符**，超过
    `maxLength: 24` —— 它会被判为"超长"并丢弃、退化成兜底文案。这里把事实
    钉成用例：**要么把上限放宽到 ≥26，要么把提示词里的例子换短**。
    当前实现选择"按硬上限收口"（≤24 是数据契约里写死的），并把冲突暴露出来。
    """
    example = "读取 user_profile.py 的前 50 行"
    assert len(example) == 26 > INTENT_MAX_CHARS
    assert normalize_intent(example, "read_file") is None
    # 同一个意图只要缩短到 24 以内就通过（说明丢的是长度，不是语义）
    short = "读取 profile.py 的前 50 行"
    assert len(short) <= INTENT_MAX_CHARS
    assert normalize_intent(short, "read_file") == short


@pytest.mark.parametrize("raw", [
    None,                            # 缺失
    123,                             # 类型不对
    "",                              # 空
    "   ",                           # 全空白
    "第一行\n第二行",                 # 换行
    "带\t制表符",                    # 控制字符
    "一" * (INTENT_MAX_CHARS + 1),   # 超长（25 字）
    "probe_echo",                    # 等于工具名
    '{"path": "/a"}',                # JSON 对象
    "[1, 2, 3]",                     # JSON 数组
    "probe_echo(path='/a')",         # 像一次工具调用
    "正在调用工具",                   # 套话
    "包含 _intent 的字样",
])
def test_normalize_rejects_invalid(raw):
    """非法标签一律丢弃 → 调用方走兜底（验收 ②④）。"""
    assert normalize_intent(raw, "probe_echo") is None


def test_normalize_rejects_secret_leaks():
    # 参数名像凭据 + 值原样出现在标签里
    assert normalize_intent("带上 tok_abcdefgh1234 去请求", "t",
                            {"api_key": "tok_abcdefgh1234"}) is None
    # 值本身长得像凭据
    assert normalize_intent("使用 sk-abcdefghijklmn 搜索", "t") is None
    # 任何完整 URL 都不许抄进标签
    assert normalize_intent("访问 https://api.example.com/data?x=1", "t") is None


def test_catalog_contract_and_unknown_tools():
    """目录项字段齐全；未知工具走兜底且不抛异常（"新增工具不得让前端报错"）。"""
    keys = {"display_name", "icon", "category", "expected_duration_ms", "retryable"}
    for name, entry in TOOL_CATALOG.items():
        assert set(entry) == keys, name
        assert entry["display_name"], name
    unknown = catalog_entry("brand_new_tool_2026")
    assert set(unknown) == keys
    assert unknown["display_name"] == "brand_new_tool_2026"
    assert unknown["category"] == "other"
    assert fallback_label("brand_new_tool_2026") == "调用 brand_new_tool_2026"
    assert fallback_label("read_file") == "调用 读取文件"


def test_resolve_intent_strips_field_and_reports_source():
    clean, label, source = resolve_intent(
        "probe_read", {"file_path": "/a.md", INTENT_FIELD: "读取 a.md 的前 20 行"}
    )
    assert clean == {"file_path": "/a.md"}          # `_intent` 被摘掉
    assert label == "读取 a.md 的前 20 行" and source == SOURCE_MODEL

    clean, label, source = resolve_intent("probe_read", {"file_path": "/a.md"})
    assert clean == {"file_path": "/a.md"}
    assert label == fallback_label("probe_read") and source == SOURCE_FALLBACK

    # 非法值 → 兜底（而不是"原样保留"）
    _, label, source = resolve_intent("probe_read",
                                      {"file_path": "/a", INTENT_FIELD: "x" * 99})
    assert source == SOURCE_FALLBACK and len(label) <= INTENT_MAX_CHARS


def test_strip_intent_tolerates_non_dict_args():
    assert strip_intent(None) == ({}, None)
    assert strip_intent("oops") == ({}, None)
    clean, raw = strip_intent({INTENT_FIELD: "写点什么", "a": 1})
    assert clean == {"a": 1} and raw == "写点什么"


def test_intent_prompt_section_is_locale_aware():
    zh = render_intent_prompt_section("zh-CN")
    assert "_intent" in zh and "现在进行时" in zh and "20 字以内" in zh
    en = render_intent_prompt_section("en-US")
    assert "_intent" in en and "Present continuous" in en
    # 未知 locale 收敛到中文（本项目默认中文用户）
    assert normalize_locale("fr") == "zh" and normalize_locale(None) == "zh"
    assert normalize_locale("en") == "en"


def test_system_prompt_carries_intent_section():
    from app.agent.prompt import build_system_prompt

    prompt = build_system_prompt()
    assert "_intent" in prompt and "意图标签" in prompt
    # 提示词段落与 schema 字段名必须一致，否则模型会写到一个不存在的字段上
    assert INTENT_FIELD in prompt


# ============================================================================
# 第 2 层：schema 注入 + 集中剥离
# ============================================================================


def test_augment_tool_schema_adds_required_intent():
    augmented = augment_tool_schema(probe_echo)
    assert augmented is not None and augmented is not probe_echo

    schema = augmented.tool_call_schema
    assert isinstance(schema, dict)                    # 直接可用作 JSON Schema
    assert INTENT_FIELD in schema["properties"]
    prop = schema["properties"][INTENT_FIELD]
    assert prop["type"] == "string" and prop["maxLength"] == INTENT_MAX_CHARS
    assert INTENT_FIELD in schema["required"]          # required 列表同步
    assert "value" in schema["properties"]             # 原有字段一个不少

    # 原始工具**完全没被改动** —— 否则 `_intent` 会变成真实必填参数，
    # 模型漏给一次工具就校验失败（硬约束：标签缺失也必须能执行）
    original = probe_echo.tool_call_schema.model_json_schema()
    assert INTENT_FIELD not in original["properties"]
    assert INTENT_FIELD not in original.get("required", [])


def test_augment_skips_raw_json_schema_tools():
    """MCP 适配器工具（args_schema 是远端原始 JSON Schema）→ 返回 None，走兜底。"""
    mcp_like = StructuredTool.from_function(
        func=lambda **kwargs: "",
        name="mcp_like",
        description="远端工具",
        args_schema={"type": "object", "properties": {"q": {"type": "string"}}},
    )
    assert augment_tool_schema(mcp_like) is None
    assert augment_tool_schema("not-a-tool") is None


def _fake_request(args: dict) -> ToolCallRequest:
    return ToolCallRequest(
        tool_call={"id": "c1", "name": "probe_echo", "args": args},
        tool=None, state={}, runtime=None,
    )


def test_intent_middleware_strips_before_handler():
    """验收 ③（单元级）：执行前把 `_intent` 摘掉，工具实现只看到业务参数。"""
    mw = IntentMiddleware()
    seen: dict = {}

    def handler(req):
        seen.update(req.tool_call["args"])
        return ToolMessage(content="ok", tool_call_id=req.tool_call["id"])

    mw.wrap_tool_call(_fake_request({"value": "a", INTENT_FIELD: "读取 a"}), handler)
    assert seen == {"value": "a"}

    # 异步链路（deepagents 真正走的那条）
    seen.clear()

    async def _run():
        async def ahandler(req):
            seen.update(req.tool_call["args"])
            return ToolMessage(content="ok", tool_call_id=req.tool_call["id"])

        return await mw.awrap_tool_call(
            _fake_request({"value": "b", INTENT_FIELD: "写 b"}), ahandler)

    asyncio.run(_run())
    assert seen == {"value": "b"}

    # 没有 `_intent` 时不复制对象（热路径上不做无谓的工作）
    plain = _fake_request({"value": "c"})
    assert mw._cleaned(plain) is plain


def test_intent_middleware_without_advertise_keeps_schemas_untouched():
    """子智能体那份（advertise=False）：只剥离，不动 schema。"""
    mw = IntentMiddleware(advertise=False)
    seen: dict = {}

    def handler(req):
        seen.update(req.tool_call["args"])
        return ToolMessage(content="ok", tool_call_id=req.tool_call["id"])

    mw.wrap_tool_call(_fake_request({"value": "a", INTENT_FIELD: "x"}), handler)
    assert seen == {"value": "a"}

    class _Req:
        tools = [probe_echo]

        def override(self, **kw):  # noqa: ANN003
            self.overridden = kw
            return self

    req = _Req()
    mw.wrap_model_call(req, lambda r: r)
    assert req.overridden["tools"] == [probe_echo]     # 原样传下去


def test_advertise_middleware_rewrites_model_tools():
    """注入那一半：送模型的 tools 里带上了 `_intent`（且不改原工具）。"""
    mw = IntentMiddleware(advertise=True)

    class _Req:
        tools = [probe_echo]

        def override(self, **kw):  # noqa: ANN003
            return _ReqView(kw["tools"])

    class _ReqView:
        def __init__(self, tools):
            self.tools = tools

    out = mw.wrap_model_call(_Req(), lambda r: r)
    assert len(out.tools) == 1
    assert INTENT_FIELD in out.tools[0].tool_call_schema["properties"]
    assert INTENT_FIELD not in probe_echo.tool_call_schema.model_json_schema()["properties"]


# ============================================================================
# 端到端（脚本化模型）：事件流 + 落库 + 投影 + 回放
# ============================================================================


def test_agent_event_stream_labels_every_tool_call():
    """验收 ①②③④：一次运行里 2 个工具调用 —— 一个给 `_intent`、一个不给。

    - 两个 `tool_call` 事件都必须带非空 `intent_label`；
    - 一个走 model、一个走 fallback；
    - 假工具收到的参数里**绝不含** `_intent`；
    - 落库与投影与事件三处一致。
    """
    sid = uuid.uuid4().hex
    uid = "u-" + uuid.uuid4().hex[:8]
    seen_args: list[dict] = []

    @tool("probe_echo")
    def recording_echo(value: str) -> str:
        """记录收到的参数，供断言"参数里有没有 `_intent`"。"""
        seen_args.append({"value": value})
        return f"echo:{value}"

    agent = _make_agent(_two_call_script(), tools=[recording_echo])
    try:
        events = _run_events(agent, sid, uid)
        calls = _tool_calls(events)
        assert len(calls) == 2, calls
        assert all(c.get("intent_label") for c in calls)

        by_id = {c["id"]: c for c in calls}
        assert by_id["call_a"]["intent_label"] == "读取 a 文件"
        assert by_id["call_a"]["intent_source"] == SOURCE_MODEL
        assert by_id["call_b"]["intent_label"] == fallback_label("probe_echo")
        assert by_id["call_b"]["intent_source"] == SOURCE_FALLBACK

        # 验收 ②：长度 ≤24 / 单行 / 不等于工具名
        for c in calls:
            label = c["intent_label"]
            assert 0 < len(label) <= INTENT_MAX_CHARS
            assert "\n" not in label and "\r" not in label
            assert label != c["name"]
            # 事件里的参数是干净的
            assert INTENT_FIELD not in json.dumps(c["args"], ensure_ascii=False)

        # 验收 ③：工具实现只看到业务参数
        assert seen_args == [{"value": "a"}, {"value": "b"}]

        # 事件 / 事件存储 / 读模型三处一致
        db = SessionLocal()
        try:
            rows = (db.query(ToolCallLog).filter(ToolCallLog.session_id == sid)
                    .order_by(ToolCallLog.id.asc()).all())
            assert [r.call_id for r in rows] == ["call_a", "call_b"]
            for row, call in zip(rows, calls):
                assert INTENT_FIELD not in (row.args or "")
                assert row.intent_label == call["intent_label"]
                assert row.intent_source == call["intent_source"]
            steps = (db.query(TaskStep).filter(TaskStep.session_id == sid)
                     .order_by(TaskStep.step_index.asc()).all())
            assert [s.step_index for s in steps] == [1, 2]
            assert [s.intent_label for s in steps] == [c["intent_label"] for c in calls]
            assert [s.intent_source for s in steps] == [c["intent_source"] for c in calls]
        finally:
            db.close()
    finally:
        _cleanup_session(sid)


def test_replay_and_projection_agree_with_the_event():
    """验收 ⑤：回放（`load_history`）两次得到同样的标签，且与实时事件一致。"""
    sid = uuid.uuid4().hex
    uid = "u-" + uuid.uuid4().hex[:8]
    agent = _make_agent(_two_call_script())
    try:
        with _patch_agent(agent):
            events = _run_events(agent, sid, uid)
            live = {c["id"]: c["intent_label"] for c in _tool_calls(events)}

            async def _history():
                return await runtime.load_history(sid, user_id=uid)

            first = asyncio.run(_history())
            second = asyncio.run(_history())

        def _labels(history):
            out = {}
            for row in history:
                for tc in row.get("toolCalls") or []:
                    out[tc["id"]] = tc.get("intentLabel")
            return out

        assert _labels(first) == _labels(second) == live
        assert all(v for v in live.values())

        # `_intent` 不许出现在回放的参数里（它是模型侧字段，不是工具参数）
        for row in first:
            for tc in row.get("toolCalls") or []:
                assert INTENT_FIELD not in json.dumps(tc["args"], ensure_ascii=False)
    finally:
        _cleanup_session(sid)


def test_invalid_intent_still_runs_tool():
    """验收 ④：`_intent` 置空 / 超长 / 塞 JSON / 写成工具名，agent 照样跑完。"""
    cases = [
        {"value": "a", INTENT_FIELD: ""},
        {"value": "a", INTENT_FIELD: "超" * 200},
        {"value": "a", INTENT_FIELD: '{"value": "a"}'},
        {"value": "a", INTENT_FIELD: "probe_echo"},
        {"value": "a", INTENT_FIELD: "第一行\n第二行"},
    ]
    for bad in cases:
        sid = uuid.uuid4().hex
        uid = "u-" + uuid.uuid4().hex[:8]
        script = [
            AIMessage(content="", tool_calls=[
                {"name": "probe_echo", "args": bad, "id": "call_x", "type": "tool_call"},
            ]),
            AIMessage(content="完成了"),
        ]
        agent = _make_agent(script)
        try:
            events = _run_events(agent, sid, uid)
            calls = _tool_calls(events)
            results = [d for k, d in events if k == "tool_result"]
            assert len(calls) == 1 and len(results) == 1
            assert calls[0]["intent_label"] == fallback_label("probe_echo")
            assert calls[0]["intent_source"] == SOURCE_FALLBACK
            assert INTENT_FIELD not in json.dumps(calls[0]["args"])
            # 工具真的执行了（返回值来自真实工具实现）
            assert "echo:a" in results[0]["content"]
        finally:
            _cleanup_session(sid)


def test_replay_is_deterministic():
    """验收 ⑤（纯函数级）：同一份参数重放两次，标签与来源逐字节相同。"""
    args = {"path": "/workspace/a.md", "pattern": "*.md",
            INTENT_FIELD: "查看工作区里的 md 文件"}
    assert resolve_intent("glob", args) == resolve_intent("glob", args)
    assert resolve_intent("glob", {"path": "/x"}) == resolve_intent("glob", {"path": "/x"})
    assert deterministic_label("read_file", {"file_path": "/a/b.py"}) == \
        deterministic_label("read_file", {"file_path": "/a/b.py"})


# ============================================================================
# 投影 / 回填
# ============================================================================


def test_projection_is_idempotent_and_matches_rebuild():
    sid = uuid.uuid4().hex
    uid = "u-" + uuid.uuid4().hex[:8]
    db = SessionLocal()
    try:
        for i, (name, label) in enumerate([("read_file", "读取 a.py"), ("ls", None)], start=1):
            db.add(ToolCallLog(
                session_id=sid, user_id=uid, call_id=f"c{i}", name=name, kind="tool",
                args="{}", status="done", intent_label=label,
                intent_source=SOURCE_MODEL if label else None,
            ))
        db.commit()

        assert project_session(db, sid) == {"inserted": 2, "updated": 0, "deleted": 0}
        db.commit()
        # 再跑一次：内容一致 → 0 改动（幂等）
        assert project_session(db, sid) == {"inserted": 0, "updated": 0, "deleted": 0}
        db.commit()

        # 单条 upsert 与全量重建的结果必须一致（两条写入路径不许分叉）
        db.query(TaskStep).filter(TaskStep.session_id == sid).delete()
        db.commit()
        project_tool_call(db, session_id=sid, user_id=uid, call_id="c1",
                          tool_name="read_file", status="running",
                          intent_label="读取 a.py", intent_source=SOURCE_MODEL)
        project_tool_call(db, session_id=sid, user_id=uid, call_id="c2",
                          tool_name="ls", status="done")
        project_tool_call(db, session_id=sid, call_id="c1", tool_name="read_file",
                          status="done")
        db.commit()
        steps = (db.query(TaskStep).filter(TaskStep.session_id == sid)
                 .order_by(TaskStep.step_index.asc()).all())
        assert [(s.call_id, s.step_index, s.status, s.intent_label) for s in steps] == [
            ("c1", 1, "done", "读取 a.py"),
            ("c2", 2, "done", None),
        ]
        # 标签"只增不减"：结果回填不会把它抹掉
        assert steps[0].intent_source == SOURCE_MODEL

        # 源数据里被删掉的调用 → 投影里的孤儿行同步消失
        db.query(ToolCallLog).filter(ToolCallLog.session_id == sid,
                                    ToolCallLog.call_id == "c2").delete()
        db.commit()
        assert project_session(db, sid) == {"inserted": 0, "updated": 0, "deleted": 1}
        db.commit()
    finally:
        db.close()
        _cleanup_session(sid)


def test_steps_endpoint_returns_intent_labels(auth_client, auth_user):
    """看板读模型通过 API 暴露出来（前端"当前步骤"的数据源）。"""
    sid = uuid.uuid4().hex
    db = SessionLocal()
    try:
        db.add(DbSession(id=sid, user_id=auth_user["id"], title=" 看板", status="active"))
        db.add(ToolCallLog(session_id=sid, user_id=auth_user["id"], call_id="c1",
                           name="read_file", kind="tool", args="{}", status="done",
                           intent_label="读取 main.py", intent_source=SOURCE_MODEL))
        db.commit()
        project_session(db, sid)
        db.commit()
    finally:
        db.close()
    try:
        resp = auth_client.get(f"/api/v1/sessions/{sid}/steps")
        assert resp.status_code == 200, resp.text
        steps = resp.json()["steps"]
        assert [s["intent_label"] for s in steps] == ["读取 main.py"]
        assert steps[0]["intent_source"] == SOURCE_MODEL
        assert steps[0]["display_name"] == "读取文件"      # 目录里的展示名
        assert steps[0]["call_id"] == "c1"
    finally:
        db = SessionLocal()
        try:
            db.query(DbSession).filter(DbSession.id == sid).delete()
            db.commit()
        finally:
            db.close()
        _cleanup_session(sid)


def test_steps_endpoint_requires_ownership(auth_client, anon_client):
    """新端点同样受归属校验约束（未登录 401 / 不存在或非本人 404）。"""
    sid = uuid.uuid4().hex
    assert anon_client.get(f"/api/v1/sessions/{sid}/steps").status_code == 401
    assert auth_client.get(f"/api/v1/sessions/{sid}/steps").status_code == 404


def test_feature_switch_off_keeps_parameters_clean():
    """`INTENT_LABEL_ENABLED=false`：不注入 schema、不给标签，但**照样剥离**。

    这一条是"展示层增强不许影响执行"的极端形式：功能关掉之后，历史消息里
    残留的 `_intent` 仍然不能在执行时到达工具。
    """
    from app.agent.prompt import build_system_prompt
    from app.core.config import settings

    sid = uuid.uuid4().hex
    uid = "u-" + uuid.uuid4().hex[:8]
    script = [
        AIMessage(content="", tool_calls=[
            {"name": "probe_echo", "args": {"value": "a", INTENT_FIELD: "读取 a"},
             "id": "call_x", "type": "tool_call"},
        ]),
        AIMessage(content="完成"),
    ]
    agent = _make_agent(script)
    settings.intent_label_enabled = False
    try:
        assert "_intent" not in build_system_prompt()
        events = _run_events(agent, sid, uid)
    finally:
        settings.intent_label_enabled = True

    try:
        calls = _tool_calls(events)
        results = [d for k, d in events if k == "tool_result"]
        assert calls[0]["intent_label"] is None and calls[0]["intent_source"] is None
        assert INTENT_FIELD not in json.dumps(calls[0]["args"])
        assert "echo:a" in results[0]["content"]      # 工具正常运行
    finally:
        _cleanup_session(sid)


def test_frontend_escapes_intent_label():
    """意图标签是模型生成的文本 → 前端只能转义渲染，绝不能当 HTML/Markdown。

    用源码断言而不是运行时断言，是因为前端没有单测基建，而这条一旦被改成
    `v-html` 就是一次 XSS（一句 `<img src=x onerror=...>` 就够了）。
    """
    src = (Path(__file__).resolve().parent.parent.parent
           / "frontend" / "src" / "App.vue").read_text(encoding="utf-8")
    assert "{{ g.intent }}" in src
    assert "{{ activityLatest(m).intent }}" in src
    for match in re.finditer(r'v-html="([^"]*)"', src):
        assert "intent" not in match.group(1).lower(), \
            f"意图标签不允许走 v-html：{match.group(1)}"
    # 渲染层要能同时读实时事件（snake_case）与回放（camelCase）两种字段名
    assert "intent_label" in src and "intentLabel" in src


def test_backfill_is_idempotent():
    """验收 ⑥：回填脚本连跑两次，第二次不产生任何改动。"""
    from scripts.backfill_intent_labels import backfill

    sid = uuid.uuid4().hex
    uid = "u-" + uuid.uuid4().hex[:8]
    db = SessionLocal()
    try:
        db.add(ToolCallLog(session_id=sid, user_id=uid, call_id="c1", name="read_file",
                           kind="tool", args=json.dumps({"file_path": "/ws/main.py"}),
                           status="done"))
        db.add(ToolCallLog(session_id=sid, user_id=uid, call_id="c2", name="unknown_tool_x",
                           kind="tool", args="{}", status="done"))
        db.commit()
    finally:
        db.close()

    try:
        first = backfill(session_id=sid)
        assert first["scanned"] == 2 and first["labeled"] == 2
        assert first["sources"] == {SOURCE_BACKFILL: 2}

        second = backfill(session_id=sid)
        assert second["scanned"] == 0, "第二次不该再有缺标签的记录"
        assert second["labeled"] == 0 and second["steps_changed"] == 0

        db = SessionLocal()
        try:
            rows = (db.query(ToolCallLog).filter(ToolCallLog.session_id == sid)
                    .order_by(ToolCallLog.id.asc()).all())
            assert all(r.intent_source == SOURCE_BACKFILL for r in rows)
            assert rows[0].intent_label == "读取 main.py"
            assert rows[1].intent_label == fallback_label("unknown_tool_x")
            # 回填结果与确定性标签函数逐字节一致（可复现）
            for r in rows:
                assert r.intent_label == deterministic_label(r.name, json.loads(r.args or "{}"))
            steps = (db.query(TaskStep).filter(TaskStep.session_id == sid)
                     .order_by(TaskStep.step_index.asc()).all())
            assert [s.intent_source for s in steps] == [SOURCE_BACKFILL, SOURCE_BACKFILL]
        finally:
            db.close()
    finally:
        _cleanup_session(sid)
