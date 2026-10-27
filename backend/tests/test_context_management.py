"""Long-run context regressions using real middleware and offline models."""
import asyncio

import pytest
from deepagents.backends import FilesystemBackend
from langchain.agents.middleware.types import ModelRequest, ModelResponse
from langchain_core.messages import AIMessage, HumanMessage, SystemMessage

from app.agent.context import build_summary_middleware
from app.agent.context_budget import estimate_tokens, resolve_budget
from app.agent.history_model import HistoryModel


def test_budget_accounts_for_output_and_unknown_models():
    budget = resolve_budget(HistoryModel(), {"context_window_tokens": 16000, "context_output_reserve": 2000})
    assert budget.input_limit == 12976
    assert budget.trigger < budget.input_limit
    assert budget.source == "configured"
    assert resolve_budget(HistoryModel()).source == "fallback"
    with pytest.raises(ValueError, match="上下文窗口不足"):
        resolve_budget(HistoryModel(), {"context_window_tokens": 4096})


def test_request_budget_includes_memory_tools_and_usage_calibration(tmp_path):
    from deepagents.middleware.memory import MemoryMiddleware
    backend = FilesystemBackend(root_dir=tmp_path, virtual_mode=True)
    memory = MemoryMiddleware(backend=backend, sources=["/Agent.md"])
    middleware = build_summary_middleware(HistoryModel(), backend=backend, request_projectors=[memory])
    request = ModelRequest(model=HistoryModel(), messages=[HumanMessage(content="你好")],
        system_message=SystemMessage(content="system"), state={"memory_contents": {"/Agent.md": "约定" * 100}},
        tools=[{"type": "function", "function": {"name": "test", "description": "参数" * 100, "parameters": {"type": "object"}}}])

    async def run():
        async def answer(sent):
            assert sent.messages == request.messages
            return ModelResponse(result=[AIMessage(content="done", usage_metadata={"input_tokens": 12000, "output_tokens": 1, "total_tokens": 12001})])
        response = await middleware.awrap_model_call(request, answer)
        usage = response.command.update["context_usage"]
        assert usage["estimated_input"] > estimate_tokens(request.messages)
        assert usage["actual_input"] == 12000
        assert usage["calibration"] > 1
    asyncio.run(run())


def test_repeated_reads_are_recoverable_without_summary(tmp_path):
    from langchain_core.messages import ToolMessage
    backend = FilesystemBackend(root_dir=tmp_path, virtual_mode=True)
    middleware = build_summary_middleware(HistoryModel(), backend=backend, session_id="duplicate")
    messages = [HumanMessage(content="Do not change public APIs", id="user")]
    for i in range(8):
        messages.extend([
            AIMessage(content="", tool_calls=[{"name": "read_file", "args": {"file_path": "/a.py", "_intent": f"读取说明 {i}"}, "id": str(i)}]),
            ToolMessage(content="same source\n" * 700, tool_call_id=str(i)),
        ])
    middleware._lc_helper.summarizer = lambda _: pytest.fail("Cheap cleaning should avoid the summary call")

    async def run():
        async def answer(sent):
            assert len(sent.messages) == len(messages)
            assert sent.messages[0].content == messages[0].content
            assert sent.messages[-1].content == messages[-1].content
            assert "context_results" in sent.messages[2].content
            return ModelResponse(result=[AIMessage(content="done")])
        response = await middleware.awrap_model_call(ModelRequest(model=HistoryModel(), messages=messages), answer)
        assert response.command.update["context_usage"]["deduplicated_reads"] == 7
        assert not response.command.update["context_usage"]["compacted"]
        assert all(m.content == "same source\n" * 700 for m in messages if isinstance(m, ToolMessage))
        assert next(tmp_path.rglob("*.txt")).read_text(encoding="utf-8") == messages[-1].content
    asyncio.run(run())


def test_failed_offload_keeps_original_and_oversized_system_is_rejected(tmp_path):
    from app.agent.context_cleaner import ContextCleaner
    from langchain_core.messages import ToolMessage
    class BrokenBackend:
        async def aupload_files(self, files):
            raise OSError("disk unavailable")
    async def run():
        original = ToolMessage(content="x" * 20000, tool_call_id="tool")
        cleaned, stats = await ContextCleaner(BrokenBackend(), "s", 1000).clean([original], pressure=True)
        assert cleaned[0] is original and stats["offloaded_results"] == 0
        middleware = build_summary_middleware(HistoryModel(), backend=FilesystemBackend(root_dir=tmp_path, virtual_mode=True))
        async def forbidden(request):
            pytest.fail("Oversized system prompts must fail before the provider call")
        with pytest.raises(ValueError, match="上下文仍超过输入预算"):
            await middleware.awrap_model_call(ModelRequest(model=HistoryModel(), messages=[HumanMessage(content="hello")],
                system_message=SystemMessage(content="x" * 100000)), forbidden)
    asyncio.run(run())


def test_provider_overflow_compacts_retries_and_preserves_state(tmp_path):
    from app.agent.context import EnhancedSummarizationMiddleware
    from langchain_core.exceptions import ContextOverflowError
    middleware = EnhancedSummarizationMiddleware(HistoryModel(), backend=FilesystemBackend(root_dir=tmp_path, virtual_mode=True),
        trigger=("messages", 100), keep=("messages", 2), summarizer=lambda _: "goal and constraints")
    messages = [message for i in range(4) for message in (HumanMessage(content=f"request {i}"), AIMessage(content=f"answer {i}"))]
    calls = []
    async def run():
        async def answer(request):
            calls.append(request.messages)
            if len(calls) == 1:
                raise ContextOverflowError("provider overflow")
            return ModelResponse(result=[AIMessage(content="resumed")])
        response = await middleware.awrap_model_call(ModelRequest(model=HistoryModel(), messages=messages), answer)
        assert len(calls) == 2 and len(calls[1]) < len(calls[0])
        assert response.command.update["_summarization_event"]["file_path"]
        assert "messages" not in response.command.update
    asyncio.run(run())


def test_full_runtime_accounts_for_assembled_prompt_and_all_tools(monkeypatch):
    from app.agent import runtime
    from langchain_core.language_models.fake_chat_models import FakeMessagesListChatModel
    from pydantic import Field
    from tests.helpers import create_user

    class RecordingModel(FakeMessagesListChatModel):
        schemas: list = Field(default_factory=list)
        actual_requests: list = Field(default_factory=list)

        def bind_tools(self, tools, **kwargs):
            self.schemas = list(tools)
            return self

        def _generate(self, messages, stop=None, run_manager=None, **kwargs):
            self.actual_requests.append(messages)
            return super()._generate(messages, stop=stop, run_manager=run_manager, **kwargs)

    user = create_user()
    sid = runtime.create_session(user["id"])
    model = RecordingModel(responses=[AIMessage(content="done")])
    monkeypatch.setattr(runtime, "_resolve_provider_for_agent", lambda *args: None)
    monkeypatch.setattr(runtime, "build_llm", lambda *args: model)
    async def run():
        agent = await runtime.get_agent(sid, user_id=user["id"])
        config = {"configurable": {"thread_id": sid}}
        await agent.ainvoke({"messages": [HumanMessage(content="你好，直接回答即可")]}, config)
        snapshot = await agent.aget_state(config)
        usage = snapshot.values["context_usage"]
        actual_estimate = estimate_tokens(model.actual_requests[-1], tools=model.schemas)
        assert 0.95 <= usage["estimated_input"] / actual_estimate <= 1.1
        assert usage["estimated_input"] < usage["input_limit"]
        names = {getattr(item, "name", None) if not isinstance(item, dict) else item.get("function", item).get("name") for item in model.schemas}
        assert {"search_history", "read_history", "remember", "update_task_state"} <= names
    try:
        asyncio.run(run())
    finally:
        runtime.invalidate_agents(sid)


def test_legacy_pending_approval_resumes_after_summary_upgrade(tmp_path):
    from deepagents import create_deep_agent
    from langchain.agents.middleware import SummarizationMiddleware as LegacySummary
    from langchain_core.language_models.fake_chat_models import FakeMessagesListChatModel
    from langgraph.checkpoint.memory import InMemorySaver
    from langgraph.types import Command
    from app.agent.context import ConversationArchiveMiddleware

    class Legacy(LegacySummary):
        @property
        def name(self):
            return "SummarizationMiddleware"
        async def abefore_agent(self, state, runtime):
            return None
        async def aafter_model(self, state, runtime):
            return None
        async def aafter_agent(self, state, runtime):
            return None

    class Model(FakeMessagesListChatModel):
        def bind_tools(self, tools, **kwargs):
            return self

    model = Model(responses=[AIMessage(content="", tool_calls=[{"name": "write_file", "id": "write-once",
        "args": {"file_path": "/note.txt", "content": "approved"}}]), AIMessage(content="done")])
    backend = FilesystemBackend(root_dir=tmp_path, virtual_mode=True)
    saver = InMemorySaver()
    def graph(middleware):
        return create_deep_agent(model=model, backend=backend, checkpointer=saver,
            middleware=middleware, interrupt_on={"write_file": True})
    async def run():
        config = {"configurable": {"thread_id": "legacy-approval"}}
        old = graph([Legacy(model, trigger=("messages", 80), keep=("messages", 24))])
        await old.ainvoke({"messages": [HumanMessage(content="保存文件", id="original-user")]}, config)
        assert (await old.aget_state(config)).interrupts
        assert not (tmp_path / "note.txt").exists()
        upgraded = graph([ConversationArchiveMiddleware("legacy-approval"), build_summary_middleware(model, backend=backend)])
        assert (await upgraded.aget_state(config)).interrupts
        await upgraded.ainvoke(Command(resume={"decisions": [{"type": "approve"}]}), config)
        assert (tmp_path / "note.txt").read_text(encoding="utf-8") == "approved"
        state = await upgraded.aget_state(config)
        assert sum(isinstance(m, HumanMessage) and m.id == "original-user" for m in state.values["messages"]) == 1
        assert sum(getattr(m, "tool_call_id", None) == "write-once" for m in state.values["messages"]) == 1
    asyncio.run(run())
