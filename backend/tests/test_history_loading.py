"""History must use graph state to reconstruct delta-backed message channels."""
import asyncio
import pytest
from types import SimpleNamespace
from langchain_core.messages import HumanMessage, AIMessage
from app.agent import runtime, context


@pytest.fixture(autouse=True)
def isolated_history(monkeypatch, tmp_path):
    asyncio.run(context.aclose())
    monkeypatch.setattr(context.settings, "sqlite_path", str(tmp_path / "history.sqlite"))
    yield
    asyncio.run(context.aclose())


def test_history_reconstructs_graph_messages(monkeypatch):
    calls = []
    class Agent:
        async def aget_state(self, config):
            calls.append(config["configurable"]["thread_id"])
            return SimpleNamespace(values={"messages": [HumanMessage(content="原会话问题"), AIMessage(content="原会话回复")]})
    async def agent(*args, **kwargs):
        return Agent()
    monkeypatch.setattr(runtime, "get_agent", agent)
    monkeypatch.setattr(runtime, "_reap_stale_if_idle", lambda sid: None)
    monkeypatch.setattr(runtime, "_load_tool_log", lambda sid: [])
    result = asyncio.run(runtime.load_history("original-session"))
    assert calls == ["original-session"]
    assert [m["content"] for m in result] == ["原会话问题", "原会话回复"]


def test_memory_notices_are_excluded_from_display_archive_and_search():
    from app.agent.conversation_history import archive_messages, read_messages, search_archive
    async def check():
        await archive_messages("memory-notice", [HumanMessage(content="real user", id="u"),
            HumanMessage(content="INTERNAL_MEMORY_UPDATE", id="notice", additional_kwargs={"lc_source": "workspace_memory"}),
            AIMessage(content="real answer", id="a")])
        assert [m.content for m in await read_messages("memory-notice")] == ["real user", "real answer"]
        assert await search_archive(["memory-notice"], "INTERNAL_MEMORY_UPDATE") == []
    asyncio.run(check())

def test_offline_graph_reconstructs_delta_messages(monkeypatch, tmp_path):
    from langgraph.checkpoint.memory import InMemorySaver
    from langgraph.store.memory import InMemoryStore
    saver = InMemorySaver()
    async def get_saver(): return saver
    async def get_store(): return InMemoryStore()
    def forbidden(*args, **kwargs):
        raise AssertionError("History must not initialize providers, MCP, or seed files")
    monkeypatch.setattr(runtime, "session_context", lambda sid: (None, None))
    monkeypatch.setattr(runtime, "session_workspace", lambda *args: tmp_path)
    monkeypatch.setattr(runtime, "_resolve_provider_for_agent", forbidden)
    monkeypatch.setattr(runtime, "build_mcp_tools", forbidden)
    monkeypatch.setattr(runtime, "seed_skills", forbidden)
    monkeypatch.setattr(runtime, "get_checkpointer", get_saver)
    monkeypatch.setattr(runtime, "get_store", get_store)
    async def check():
        graph = await runtime.get_agent("offline-history-test", history_only=True)
        config = {"configurable": {"thread_id": "offline-history-test"}}
        for i in range(3):
            await graph.aupdate_state(config, {"messages": [HumanMessage(content=f"question {i}", id=f"user-{i}"), AIMessage(content=f"answer {i}", id=f"assistant-{i}")]}, as_node="model")
        snapshot = await saver.aget_tuple(config)
        assert "messages" not in snapshot.checkpoint["channel_values"]
        state = await graph.aget_state(config)
        assert [m.content for m in state.values["messages"]] == [text for i in range(3) for text in (f"question {i}", f"answer {i}")]
        assert "offline-history-test" not in runtime._agents
        assert not (tmp_path / "Agent.md").exists()
    asyncio.run(check())


def test_repeated_summary_keeps_display_history_after_restart(monkeypatch, tmp_path):
    from langchain.agents import create_agent
    from langchain_core.language_models.fake_chat_models import FakeListChatModel
    from app.agent.conversation_history import sync_checkpoints, read_messages, is_internal_summary

    model = FakeListChatModel(responses=["正常回复"])
    from deepagents.backends import FilesystemBackend
    middleware = context.EnhancedSummarizationMiddleware(
        model, backend=FilesystemBackend(root_dir=tmp_path, virtual_mode=True), trigger=("messages", 4),
        keep=("messages", 2), summarizer=lambda messages: "internal compact context",
    )
    graph = None
    async def agent(*args, **kwargs): return graph
    monkeypatch.setattr(runtime, "get_agent", agent)
    monkeypatch.setattr(runtime, "_reap_stale_if_idle", lambda sid: None)
    monkeypatch.setattr(runtime, "_load_tool_log", lambda sid: [])

    async def check():
        nonlocal graph
        graph = create_agent(model, middleware=[context.ConversationArchiveMiddleware("summary-test"), middleware], checkpointer=await context.get_checkpointer())
        config = {"configurable": {"thread_id": "summary-test"}}
        for i in range(5):
            await sync_checkpoints(graph, "summary-test")
            from app.agent.conversation_history import archive_stream
            async for _ in archive_stream(graph.astream(
                    {"messages": [HumanMessage(content=f"原始问题 {i}")]}, config,
                    stream_mode="messages"), "summary-test"):
                pass
        state = await graph.aget_state(config)
        assert state.values["_summarization_event"]["file_path"]
        assert len(state.values["messages"]) == 10
        assert not any(is_internal_summary(m) for m in state.values["messages"])
        assert list(tmp_path.rglob('*.md'))
        result = await runtime.load_history("summary-test")
        expected = [text for i in range(5) for text in (f"原始问题 {i}", "正常回复")]
        assert [m["content"] for m in result] == expected
        # Independent SQLite archive survives a closed connection/restart.
        await context.aclose()
        assert [m.content for m in await read_messages("summary-test")] == expected
        graph = create_agent(model, middleware=[context.ConversationArchiveMiddleware("summary-test"), middleware], checkpointer=await context.get_checkpointer())
        assert [m["content"] for m in await runtime.load_history("summary-test")] == expected
        assert await read_messages("other-session") == []
    asyncio.run(check())


def test_old_checkpoints_restore_compacted_messages_and_tool_details(monkeypatch):
    from langchain.agents import create_agent
    from langchain_core.messages import ToolMessage, RemoveMessage
    from langgraph.graph.message import REMOVE_ALL_MESSAGES
    from langgraph.checkpoint.memory import InMemorySaver
    from app.agent.history_model import HistoryModel
    from app.agent.conversation_history import read_messages

    graph = create_agent(HistoryModel(), checkpointer=InMemorySaver())
    async def agent(*args, **kwargs): return graph
    monkeypatch.setattr(runtime, "get_agent", agent)
    monkeypatch.setattr(runtime, "_reap_stale_if_idle", lambda sid: None)
    monkeypatch.setattr(runtime, "_load_tool_log", lambda sid: [])
    async def check():
        config = {"configurable": {"thread_id": "old-session"}}
        original = [
            HumanMessage(content=[{"type": "text", "text": "旧问题"},
                {"type": "image_url", "image_url": {"url": "data:image/png;base64,YQ=="}}], id="u1"),
            AIMessage(content="", id="a1", tool_calls=[{"id": "tc1", "name": "read_file", "args": {"path": "/workspace/a.txt"}}]),
            ToolMessage(content="原始工具结果", tool_call_id="tc1", id="t1"),
            AIMessage(content="旧回复", id="a2"),
        ]
        await graph.aupdate_state(config, {"messages": original}, as_node="model")
        await graph.aupdate_state(config, {"messages": [
            RemoveMessage(id=REMOVE_ALL_MESSAGES),
            HumanMessage(content="Here is a summary of the conversation to date: internal",
                id="s1", additional_kwargs={"lc_source": "summarization"}),
            HumanMessage(content="新问题", id="u2"), AIMessage(content="新回复", id="a3"),
        ]}, as_node="model")
        result = await runtime.load_history("old-session")
        assert [m["content"] for m in result] == ["旧问题", "旧回复", "新问题", "新回复"]
        assert result[0]["images"] == ["data:image/png;base64,YQ=="]
        assert result[1]["toolCalls"][0]["result"] == "原始工具结果"
        assert [m["content"] for m in await runtime.load_history("old-session")] == [m["content"] for m in result]
        assert len(await read_messages("old-session")) == 6
    asyncio.run(check())


def test_stream_failure_retains_partial_output_without_internal_summary():
    from langchain_core.messages import AIMessageChunk
    from app.agent.conversation_history import archive_messages, archive_stream, read_messages

    async def chunks():
        yield AIMessageChunk(content="hidden summary", id="internal"), {"langgraph_node": "SummarizationMiddleware.before_model"}
        yield AIMessageChunk(content="hidden internal", id="internal2"), {"langgraph_node": "model", "tags": ["langchain:internal"]}
        yield AIMessageChunk(content="部分", id="reply"), {"langgraph_node": "model"}
        yield AIMessageChunk(content="回答", id="reply"), {"langgraph_node": "model"}
        raise RuntimeError("interrupted")

    async def check():
        await archive_messages("partial", [HumanMessage(content="问题", id="u")])
        with pytest.raises(RuntimeError, match="interrupted"):
            async for _ in archive_stream(chunks(), "partial"):
                pass
        messages = await read_messages("partial")
        assert [m.content for m in messages] == ["问题", "部分回答"]
    asyncio.run(check())


def test_context_rewrites_cannot_replace_archived_original():
    from app.agent.conversation_history import archive_messages, read_messages
    async def check():
        await archive_messages("rewrite", [AIMessage(content="部分", id="a")], partial=True)
        await archive_messages("rewrite", [AIMessage(content="完整原文", id="a")])
        await archive_messages("rewrite", [AIMessage(content="压缩后的工具内容", id="a")])
        await archive_messages("rewrite", [AIMessage(content="晚到的流片段", id="a")], partial=True)
        assert [m.content for m in await read_messages("rewrite")] == ["完整原文"]
    asyncio.run(check())


def test_interrupted_and_resumed_turn_has_one_user_message():
    from typing import Annotated, TypedDict
    from langgraph.graph import StateGraph, START, END
    from langgraph.graph.message import add_messages
    from langgraph.types import interrupt, Command
    from langgraph.checkpoint.memory import InMemorySaver
    from app.agent.conversation_history import sync_checkpoints, read_messages

    class State(TypedDict):
        messages: Annotated[list, add_messages]

    def answer(state):
        interrupt("approve")
        return {"messages": [AIMessage(content="审批后回复", id="a")]}

    graph = StateGraph(State)
    graph.add_node("answer", answer)
    graph.add_edge(START, "answer")
    graph.add_edge("answer", END)
    agent = graph.compile(checkpointer=InMemorySaver())
    async def check():
        config = {"configurable": {"thread_id": "approval-history"}}
        await agent.ainvoke({"messages": [HumanMessage(content="原问题", id="u")]}, config)
        await sync_checkpoints(agent, "approval-history")
        assert [m.content for m in await read_messages("approval-history")] == ["原问题"]
        await agent.ainvoke(Command(resume=True), config)
        await sync_checkpoints(agent, "approval-history")
        assert [m.content for m in await read_messages("approval-history")] == ["原问题", "审批后回复"]
    asyncio.run(check())
