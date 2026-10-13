"""History must use graph state to reconstruct delta-backed message channels."""
import asyncio
from types import SimpleNamespace
from langchain_core.messages import HumanMessage, AIMessage
from app.agent import runtime


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
    asyncio.run(check())
