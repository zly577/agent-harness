import asyncio
import json
import pytest

from deepagents.backends import FilesystemBackend

from app.agent.memory_tools import build_memory_tools
from app.agent.workspace_memory import WorkspaceMemoryRepairMiddleware


def test_detail_memory_is_indexed_without_loading_body_and_core_is_bounded(tmp_path):
    tool = build_memory_tools(tmp_path)[0]
    result = json.loads(tool.invoke({"key": "build", "title": "构建经验", "content": "DETAIL BODY MUST STAY OUT OF PROMPT",
                                    "source": "tool:build-42"}))
    assert result["saved"]
    detail = (tmp_path / ".agent-memory/build.md").read_text(encoding="utf-8")
    assert "tool:build-42" in detail and "更新时间" in detail and "当前工作区" in detail
    original = "中文核心偏好\n" + "old details" * 1000
    (tmp_path / "Agent.md").write_text(original, encoding="utf-8")
    middleware = WorkspaceMemoryRepairMiddleware(FilesystemBackend(root_dir=tmp_path, virtual_mode=True), ["/Agent.md"])
    async def run():
        update = await middleware.abefore_agent({}, None)
        content = update["memory_contents"]["/Agent.md"]
        assert "核心偏好" in content and ".agent-memory/build.md" in content
        assert "DETAIL BODY MUST STAY OUT OF PROMPT" not in content
        assert len(content) < 10000 and "仅加载前" in content
        assert (tmp_path / "Agent.md").read_text(encoding="utf-8") == original
    asyncio.run(run())


def test_memory_revision_and_core_capacity_preserve_existing_content(tmp_path):
    tool = build_memory_tools(tmp_path)[0]
    args = {"key": "language", "title": "语言", "content": "请用中文", "source": "user:message-1", "tier": "core"}
    first = json.loads(tool.invoke(args))
    assert first["saved"]
    path = tmp_path / "Agent.md"
    # A user edits the file after the agent's read.
    path.write_text(path.read_text(encoding="utf-8") + "\n用户最新修改", encoding="utf-8")
    conflict = json.loads(tool.invoke({**args, "content": "过期替换", "expected_revision": first["revision"]}))
    assert conflict["error"] == "revision_conflict"
    assert "用户最新修改" in path.read_text(encoding="utf-8")
    full = json.loads(tool.invoke({**args, "content": "x" * 7000, "expected_revision": conflict["revision"]}))
    assert full["error"] == "core_full"
    assert "用户最新修改" in path.read_text(encoding="utf-8")
    assert not (tmp_path / ".agent-memory").exists()


def test_memory_scope_and_symlink_rejection(tmp_path):
    first, second = tmp_path / "first", tmp_path / "second"
    first.mkdir(); second.mkdir()
    args = {"key": "project", "title": "项目", "content": "first-only", "source": "message:1"}
    assert json.loads(build_memory_tools(first)[0].invoke(args))["saved"]
    assert not (second / ".agent-memory").exists()
    target = tmp_path / "external.md"
    target.write_text("outside", encoding="utf-8")
    try:
        (second / "Agent.md").symlink_to(target)
    except OSError:
        return
    rejected = json.loads(build_memory_tools(second)[0].invoke({**args, "tier": "core"}))
    assert "error" in rejected
    assert target.read_text(encoding="utf-8") == "outside"


def test_memory_conflicts_are_reported_as_tool_failures(tmp_path):
    tool = build_memory_tools(tmp_path)[0]
    args = {"key": "project", "title": "约定", "content": "confirmed", "source": "user:1"}
    assert json.loads(tool.invoke(args))["saved"]
    response = tool.invoke({"type": "tool_call", "name": "remember", "id": "memory-conflict", "args": args})
    assert response.status == "error"
    assert json.loads(response.content)["error"] == "revision_conflict"


def test_memory_changes_append_once_and_preserve_baseline(tmp_path):
    path = tmp_path / "Agent.md"
    path.write_text("workspace memory", encoding="utf-8")
    middleware = WorkspaceMemoryRepairMiddleware(
        FilesystemBackend(root_dir=tmp_path, virtual_mode=True), ["/Agent.md"])

    async def check():
        # Also repair checkpoints left by the former user-wide memory route.
        update = await middleware.abefore_agent({"memory_contents": {"/memories/Agent.md": "wrong scope"}}, None)
        assert "workspace memory" in update["memory_contents"]["/Agent.md"]
        assert "wrong scope" not in str(update)
        baseline = update["memory_contents"]
        state = dict(update)
        assert "messages" not in await middleware.abefore_agent(state, None)
        path.write_text("", encoding="utf-8")
        cleared = await middleware.abefore_agent(state, None)
        assert "memory_contents" not in cleared
        assert "清空" in cleared["messages"][0].content
        state.update(cleared)
        assert "messages" not in await middleware.abefore_agent(state, None)
        path.unlink()
        deleted = await middleware.abefore_agent(state, None)
        assert "删除" in deleted["messages"][0].content
        state.update(deleted)
        path.write_text("recreated", encoding="utf-8")
        recreated = await middleware.abefore_agent(state, None)
        assert "recreated" in recreated["messages"][0].content
        assert state["memory_contents"] == baseline

    asyncio.run(check())


def test_memory_notices_use_small_diffs_and_ignore_timestamp_only_changes(tmp_path):
    path = tmp_path / "Agent.md"
    original = "".join(f"preference {i}: keep unchanged\n" for i in range(100))
    path.write_text(original, encoding="utf-8")
    detail = tmp_path / ".agent-memory" / "build.md"
    detail.parent.mkdir()
    detail.write_text("on-demand detail", encoding="utf-8")
    middleware = WorkspaceMemoryRepairMiddleware(FilesystemBackend(root_dir=tmp_path, virtual_mode=True), ["/Agent.md"])

    async def check():
        state = await middleware.abefore_agent({}, None)
        detail.touch()
        assert "messages" not in await middleware.abefore_agent(state, None)
        path.write_text(original.replace("preference 50: keep unchanged", "preference 50: use the new rule"), encoding="utf-8")
        update = await middleware.abefore_agent(state, None)
        notice = update["messages"][0].content
        assert "-preference 50: keep unchanged\n" in notice
        assert "+preference 50: use the new rule\n" in notice
        assert "preference 0:" not in notice and len(notice) < len(original)
        assert "memory_contents" not in update

    asyncio.run(check())


@pytest.mark.parametrize("external_change", [False, True])
@pytest.mark.parametrize("compact_during_tool_loop", [False, True])
def test_remember_writes_are_acknowledged_without_hiding_concurrent_edits(tmp_path, monkeypatch, external_change, compact_during_tool_loop):
    import hashlib
    from deepagents import create_deep_agent
    from langchain_core.language_models.fake_chat_models import FakeMessagesListChatModel
    from langchain_core.messages import AIMessage
    from langgraph.checkpoint.memory import InMemorySaver
    from app.agent.sandbox import build_main_backend
    from app.agent.context import EnhancedSummarizationMiddleware
    from deepagents.middleware.memory import MemoryMiddleware
    from app.core.config import settings

    monkeypatch.setattr(settings, "harness_machine_access", False)
    path = tmp_path / "Agent.md"
    path.write_text("original preference", encoding="utf-8")
    requests = []

    class Model(FakeMessagesListChatModel):
        def bind_tools(self, tools, **kwargs):
            return self

        def _generate(self, messages, stop=None, run_manager=None, **kwargs):
            if messages[0].type != "system":
                from langchain_core.outputs import ChatGeneration, ChatResult
                return ChatResult(generations=[ChatGeneration(message=AIMessage(content="Summary forgets the saved preference"))])
            requests.append(messages)
            if external_change and len(requests) == 2:
                path.write_text(path.read_text(encoding="utf-8") + "\nCONCURRENT EXTERNAL EDIT", encoding="utf-8")
            return super()._generate(messages, stop=stop, run_manager=run_manager, **kwargs)

    model = Model(responses=[AIMessage(content="", tool_calls=[{"name": "remember", "id": "remember-language", "args": {
        "key": "language", "title": "language", "content": "ANSWER IN ENGLISH", "source": "user:1", "tier": "core",
        "expected_revision": hashlib.sha256(path.read_bytes()).hexdigest()}}]), AIMessage(content="saved"), AIMessage(content="next")])
    backend = build_main_backend(workspace=tmp_path)
    sources = ["/workspace/Agent.md"]
    summary = EnhancedSummarizationMiddleware(model, backend=backend, trigger=("messages", 3), keep=("messages", 2),
        summarizer=lambda _: "Summary forgets the saved preference", request_projectors=[MemoryMiddleware(backend=backend, sources=sources)])
    agent = create_deep_agent(model=model, backend=backend, memory=["/workspace/Agent.md"],
        tools=build_memory_tools(tmp_path), middleware=[WorkspaceMemoryRepairMiddleware(backend, sources),
                                                      *([summary] if compact_during_tool_loop else [])],
        checkpointer=InMemorySaver())

    async def check():
        config = {"configurable": {"thread_id": "remember-session"}}
        await agent.ainvoke({"messages": [{"role": "user", "content": "Use English and remember it"}]}, config)
        if compact_during_tool_loop:
            state = await agent.aget_state(config)
            assert state.values["_summarization_event"]
            assert "ANSWER IN ENGLISH" in str(requests[1][0].content)
            monkeypatch.setattr(summary, "_should_summarize", lambda *_: False)
        await agent.ainvoke({"messages": [{"role": "user", "content": "Continue"}]}, config)
        state = await agent.aget_state(config)
        notices = [m for m in state.values["messages"] if m.additional_kwargs.get("lc_source") == "workspace_memory"]
        assert len(notices) == int(external_change)
        assert str(requests[1 if compact_during_tool_loop else 0][0].content) == str(requests[-1][0].content)
        if external_change:
            assert "CONCURRENT EXTERNAL EDIT" in notices[0].content
        else:
            assert "ANSWER IN ENGLISH" in state.values["workspace_memory_state"]["latest"]["/workspace/Agent.md"]["core"]

    asyncio.run(check())


def test_compaction_rebases_memory_and_removes_absorbed_notices_from_model_view(tmp_path, monkeypatch):
    from deepagents import create_deep_agent
    from deepagents.middleware.memory import MemoryMiddleware
    from langchain_core.language_models.fake_chat_models import FakeMessagesListChatModel
    from langchain_core.messages import AIMessage
    from langgraph.checkpoint.memory import InMemorySaver
    from app.agent.context import EnhancedSummarizationMiddleware
    from app.agent.sandbox import build_main_backend
    from app.core.config import settings

    monkeypatch.setattr(settings, "harness_machine_access", False)
    path = tmp_path / "Agent.md"
    path.write_text("BASELINE BEFORE COMPACTION", encoding="utf-8")
    requests = []

    class Model(FakeMessagesListChatModel):
        def bind_tools(self, tools, **kwargs):
            return self

        def _generate(self, messages, stop=None, run_manager=None, **kwargs):
            requests.append(messages)
            return super()._generate(messages, stop=stop, run_manager=run_manager, **kwargs)

    model = Model(responses=[AIMessage(content="done")])
    backend = build_main_backend(workspace=tmp_path)
    sources = ["/workspace/Agent.md"]
    summary = EnhancedSummarizationMiddleware(model, backend=backend, trigger=("messages", 4), keep=("messages", 2),
        summarizer=lambda _: "Summary omits memory changes", request_projectors=[MemoryMiddleware(backend=backend, sources=sources)])
    saver = InMemorySaver()

    def graph():
        return create_deep_agent(model=model, backend=backend, memory=sources,
            middleware=[WorkspaceMemoryRepairMiddleware(backend, sources), summary], checkpointer=saver)

    async def check():
        config = {"configurable": {"thread_id": "compact-memory"}}
        agent = graph()
        await agent.ainvoke({"messages": [{"role": "user", "content": "First turn"}]}, config)
        path.write_text("LATEST MEMORY AFTER COMPACTION", encoding="utf-8")
        await agent.ainvoke({"messages": [{"role": "user", "content": "Second turn"}]}, config)
        state = await agent.aget_state(config)
        assert state.values["_summarization_event"]
        assert "LATEST MEMORY AFTER COMPACTION" in str(requests[-1][0].content)
        assert "BASELINE BEFORE COMPACTION" not in str(requests[-1][0].content)
        assert not any(m.additional_kwargs.get("lc_source") == "workspace_memory" for m in requests[-1])
        # Notices stay in raw state so absolute summary cutoff indices stay valid.
        assert any(m.additional_kwargs.get("lc_source") == "workspace_memory" for m in state.values["messages"])
        baseline = requests[-1][0].content
        monkeypatch.setattr(summary, "_should_summarize", lambda *_: False)
        await graph().ainvoke({"messages": [{"role": "user", "content": "After rebuilding graph"}]}, config)
        assert requests[-1][0].content == baseline
        assert not any(m.additional_kwargs.get("lc_source") == "workspace_memory" for m in requests[-1])

    asyncio.run(check())


def test_real_model_prefix_stays_stable_and_new_sessions_get_latest_memory(tmp_path, monkeypatch):
    from deepagents import create_deep_agent
    from langchain_core.language_models.fake_chat_models import FakeMessagesListChatModel
    from langchain_core.messages import AIMessage
    from langchain_core.tools import tool
    from langgraph.checkpoint.memory import InMemorySaver
    from app.agent.prompt import memory_sources
    from app.agent.sandbox import build_main_backend
    from app.core.config import settings

    monkeypatch.setattr(settings, "harness_machine_access", False)
    monkeypatch.setattr(settings, "sandbox_enabled", False)
    first, second = tmp_path / "first", tmp_path / "second"
    first.mkdir(); second.mkdir()
    path = first / "Agent.md"
    path.write_text("WORKSPACE_ONE_VERSION_ONE", encoding="utf-8")
    (second / "Agent.md").write_text("WORKSPACE_TWO_MEMORY", encoding="utf-8")
    prompts = []
    requests = []
    saver = InMemorySaver()

    class Model(FakeMessagesListChatModel):
        def bind_tools(self, tools, **kwargs):
            return self

        def _generate(self, messages, stop=None, run_manager=None, **kwargs):
            prompts.append(str(messages[0].content))
            requests.append([(m.type, m.content, getattr(m, "tool_calls", None)) for m in messages])
            return super()._generate(messages, stop=stop, run_manager=run_manager, **kwargs)

    @tool
    def edit_workspace_memory() -> str:
        """Persist a confirmed new workspace preference."""
        path.write_text("WORKSPACE_ONE_VERSION_TWO", encoding="utf-8")
        return "Saved the new preference; apply it now."

    def graph(project, responses, tools=None):
        backend = build_main_backend(workspace=project)
        sources = memory_sources("user", workspace=project)
        return create_deep_agent(model=Model(responses=responses), backend=backend, memory=sources,
            tools=tools or [], middleware=[WorkspaceMemoryRepairMiddleware(backend, sources)],
            checkpointer=saver)

    async def check():
        agent = graph(first, [
            AIMessage(content="", tool_calls=[{"name": "edit_workspace_memory", "args": {}, "id": "edit-memory"}]),
            AIMessage(content="saved"), AIMessage(content="next turn"),
        ], [edit_workspace_memory])
        config = {"configurable": {"thread_id": "existing-session"}}
        await agent.ainvoke({"messages": [{"role": "user", "content": "Update my preference"}]}, config)
        assert len(prompts) == 2
        # Editing disk mid-turn must not silently mutate the system prompt in the tool loop.
        assert all(text.count("WORKSPACE_ONE_VERSION_ONE") == 1 for text in prompts)
        assert all("WORKSPACE_ONE_VERSION_TWO" not in text for text in prompts)
        await agent.ainvoke({"messages": [{"role": "user", "content": "Next request"}]}, config)
        assert prompts[-1] == prompts[0]
        assert "WORKSPACE_ONE_VERSION_TWO" not in prompts[-1]
        assert "WORKSPACE_ONE_VERSION_TWO" in str(requests[-1][-1])
        assert requests[-1][:len(requests[1])] == requests[1]
        state = await agent.aget_state(config)
        assert not any(message.type == "system" for message in state.values["messages"])
        notices = [m for m in state.values["messages"] if m.additional_kwargs.get("lc_source") == "workspace_memory"]
        assert len(notices) == 1
        # Rebuilding the graph must retain both baseline and notification cursor.
        rebuilt = graph(first, [AIMessage(content="restarted")])
        await rebuilt.ainvoke({"messages": [{"role": "user", "content": "After restart"}]}, config)
        assert prompts[-1] == prompts[0]
        state = await rebuilt.aget_state(config)
        assert len([m for m in state.values["messages"] if m.additional_kwargs.get("lc_source") == "workspace_memory"]) == 1

        fresh = graph(first, [AIMessage(content="new session")])
        await fresh.ainvoke({"messages": [{"role": "user", "content": "Hello"}]},
                           {"configurable": {"thread_id": "new-session"}})
        assert prompts[-1].count("WORKSPACE_ONE_VERSION_TWO") == 1
        other = graph(second, [AIMessage(content="other workspace")])
        await other.ainvoke({"messages": [{"role": "user", "content": "Hello"}]},
                           {"configurable": {"thread_id": "other-workspace"}})
        assert "WORKSPACE_TWO_MEMORY" in prompts[-1] and "WORKSPACE_ONE" not in prompts[-1]

    asyncio.run(check())
