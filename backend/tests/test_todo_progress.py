import asyncio
import uuid
from types import SimpleNamespace

from langchain.agents import create_agent
from langchain_core.language_models.fake_chat_models import FakeMessagesListChatModel
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage
from langgraph.checkpoint.memory import InMemorySaver
from deepagents import create_deep_agent
from deepagents.backends import FilesystemBackend

from app.agent.todo_progress import TodoProgressMiddleware, progress_error
from app.agent.tool_outcome import classify_tool_result


def todo(content, status):
    return {"content": content, "status": status}


def test_java_replay_rejects_zero_to_nine_and_keeps_the_saved_plan(tmp_path):
    """Regression: the real report had only an initial list and a 9/9 update."""
    initial = [todo(f"阶段{i}", "in_progress" if i == 0 else "pending") for i in range(9)]
    finished = [todo(item["content"], "completed") for item in initial]
    model = Model(responses=[call("initial", initial), call("batch", finished), AIMessage(content="更新被拒绝")])
    middleware = TodoProgressMiddleware()
    # Exercise DeepAgents' built-in middleware replacement, not only a bare
    # create_agent graph: the production runtime must use this guard exactly once.
    graph = create_deep_agent(model, middleware=[middleware], subagents=[],
                              backend=FilesystemBackend(root_dir=tmp_path, virtual_mode=True))
    result = asyncio.run(graph.ainvoke({"messages": [HumanMessage(content="制定 Java 学习规划")]}))
    assert result["todos"] == initial
    error = next(m for m in result["messages"] if isinstance(m, ToolMessage) and m.tool_call_id == "batch")
    assert error.status == "error"
    assert "本次清单未保存" in error.content
    assert classify_tool_result("write_todos", error.content)["ok"] is False


def test_incremental_updates_survive_checkpoint_rebuild_and_allow_parallel_starts():
    first = [todo("梳理知识模块", "in_progress"), todo("安排练习", "pending")]
    next_step = [todo("梳理知识模块", "completed"), todo("安排练习", "in_progress")]
    finished = [todo(item["content"], "completed") for item in first]
    saver = InMemorySaver()

    async def run():
        config = {"configurable": {"thread_id": "incremental-plan"}}
        graph = create_agent(Model(responses=[call("start", first), call("one-done", next_step), AIMessage(content="继续下一步")]),
                             middleware=[TodoProgressMiddleware()], checkpointer=saver)
        await graph.ainvoke({"messages": [HumanMessage(content="规划学习路线")]}, config)
        graph = create_agent(Model(responses=[call("last-done", finished), AIMessage(content="路线已交付")]),
                             middleware=[TodoProgressMiddleware()], checkpointer=saver)
        result = await graph.ainvoke({"messages": [HumanMessage(content="继续")]}, config)
        assert result["todos"] == finished
        assert not any(m.status == "error" for m in result["messages"] if isinstance(m, ToolMessage))
    asyncio.run(run())

    parallel = [todo("A", "in_progress"), todo("B", "in_progress")]
    assert progress_error([], parallel) is None
    assert "最多新增一个" in progress_error(parallel, [todo("A", "completed"), todo("B", "completed")])
    assert progress_error(parallel, [todo("A", "completed"), todo("B", "in_progress")]) is None


def test_initial_completed_renames_and_duplicate_labels_cannot_bypass_start_tracking():
    assert progress_error([], [todo("尚未开始", "completed")])
    assert progress_error([todo("旧名称", "in_progress")], [todo("新名称", "completed")])
    previous = [todo("检查", "completed"), todo("检查", "pending")]
    assert progress_error(previous, [todo("检查", "completed"), todo("检查", "completed")])
    assert progress_error(previous, [todo("检查", "completed"), todo("检查", "in_progress")]) is None
    assert progress_error([todo("已完成", "completed")], []) is None
    assert progress_error([], "malformed") is None  # Tool schema owns validation.


def test_sync_guard_passes_other_tools_through_and_preserves_todo_middleware_identity():
    middleware = TodoProgressMiddleware()
    assert middleware.name == "TodoListMiddleware"
    assert len(middleware.tools) == 1 and middleware.tools[0].name == "write_todos"
    assert "未来计划" in middleware.tools[0].description
    assert "每次最多新增一个 completed" in middleware.system_prompt
    request = SimpleNamespace(tool_call={"name": "write_todos", "id": "bad", "args": {"todos": [todo("x", "completed")]}}, state={})
    def forbidden(_):
        raise AssertionError("invalid progress must not reach the tool")
    assert middleware.wrap_tool_call(request, forbidden).status == "error"
    request.tool_call["name"] = "read_file"
    assert middleware.wrap_tool_call(request, lambda _: "passed") == "passed"


def test_runtime_stream_delivers_each_saved_step_and_marks_rejected_batch_as_failure(monkeypatch, tmp_path):
    from app.agent import runtime
    from app.agent.events import to_sse_event

    initial = [todo(f"交付步骤{i}", "in_progress" if i == 0 else "pending") for i in range(9)]
    snapshots = [initial]
    for i in range(9):
        snapshots.append([todo(item["content"], "completed" if j <= i else "in_progress" if j == i + 1 else "pending")
                          for j, item in enumerate(initial)])
    responses = [call("initial", initial), call("rejected-batch", snapshots[-1])]
    responses.extend(call(f"step-{i}", items) for i, items in enumerate(snapshots[1:]))
    responses.append(AIMessage(content="规划已交付"))
    graph = create_deep_agent(Model(responses=responses), middleware=[TodoProgressMiddleware()], subagents=[],
                              backend=FilesystemBackend(root_dir=tmp_path, virtual_mode=True), checkpointer=InMemorySaver())
    async def get_agent(*args, **kwargs):
        return graph
    logged = []
    monkeypatch.setattr(runtime, "get_agent", get_agent)
    monkeypatch.setattr(runtime, "mark_stale_tool_calls", lambda _: 0)
    monkeypatch.setattr(runtime, "_log_tool_event", lambda *args, **kwargs: logged.append((args, kwargs)))
    async def run():
        return [to_sse_event(kind, data) async for kind, data in runtime.agent_event_stream(
            uuid.uuid4().hex, {"messages": [HumanMessage(content="制定规划")]}, user_id="fixture")]
    events = asyncio.run(run())
    calls = {event["id"]: event for event in events if event["type"] == "tool_call"}
    counts = []
    for event in events:
        if event["type"] != "tool_result":
            continue
        if event["id"] == "rejected-batch":
            assert event["ok"] is False and "未保存" in event["content"]
        else:
            assert event["ok"] is True
            counts.append(sum(item["status"] == "completed" for item in calls[event["id"]]["args"]["todos"]))
    assert counts == list(range(10))
    assert any(args[2] == "rejected-batch" and kwargs.get("status") == "error" for args, kwargs in logged)


class Model(FakeMessagesListChatModel):
    def bind_tools(self, tools, **kwargs):
        return self


def call(identifier, items):
    return AIMessage(content="", tool_calls=[{"id": identifier, "name": "write_todos", "args": {"todos": items}}])
