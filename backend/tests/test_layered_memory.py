import asyncio
import json

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


def test_memory_refresh_replaces_stale_cache_and_skips_unchanged_content(tmp_path):
    path = tmp_path / "Agent.md"
    path.write_text("workspace memory", encoding="utf-8")
    middleware = WorkspaceMemoryRepairMiddleware(
        FilesystemBackend(root_dir=tmp_path, virtual_mode=True), ["/Agent.md"])

    async def check():
        # Also repair checkpoints left by the former user-wide memory route.
        update = await middleware.abefore_agent({"memory_contents": {"/memories/Agent.md": "wrong scope"}}, None)
        assert update == {"memory_contents": {"/Agent.md": "workspace memory"}}
        assert await middleware.abefore_agent(update, None) is None
        path.write_text("", encoding="utf-8")
        cleared = await middleware.abefore_agent(update, None)
        assert cleared == {"memory_contents": {"/Agent.md": ""}}
        path.unlink()
        deleted = await middleware.abefore_agent(update, None)
        assert deleted == {"memory_contents": {}}
        path.write_text("recreated", encoding="utf-8")
        assert await middleware.abefore_agent(deleted, None) == {"memory_contents": {"/Agent.md": "recreated"}}

    asyncio.run(check())


def test_real_model_memory_snapshot_refreshes_next_turn_and_in_new_session(tmp_path, monkeypatch):
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

    class Model(FakeMessagesListChatModel):
        def bind_tools(self, tools, **kwargs):
            return self

        def _generate(self, messages, stop=None, run_manager=None, **kwargs):
            prompts.append(str(messages[0].content))
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
            checkpointer=InMemorySaver())

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
        assert prompts[-1].count("WORKSPACE_ONE_VERSION_TWO") == 1
        assert "WORKSPACE_ONE_VERSION_ONE" not in prompts[-1]
        state = await agent.aget_state(config)
        assert not any(message.type == "system" for message in state.values["messages"])

        fresh = graph(first, [AIMessage(content="new session")])
        await fresh.ainvoke({"messages": [{"role": "user", "content": "Hello"}]},
                           {"configurable": {"thread_id": "new-session"}})
        assert prompts[-1].count("WORKSPACE_ONE_VERSION_TWO") == 1
        other = graph(second, [AIMessage(content="other workspace")])
        await other.ainvoke({"messages": [{"role": "user", "content": "Hello"}]},
                           {"configurable": {"thread_id": "other-workspace"}})
        assert "WORKSPACE_TWO_MEMORY" in prompts[-1] and "WORKSPACE_ONE" not in prompts[-1]

    asyncio.run(check())
