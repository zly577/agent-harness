import asyncio

from langchain.agents import create_agent
from langchain_core.language_models.fake_chat_models import FakeMessagesListChatModel
from langchain_core.messages import AIMessage, HumanMessage
from langgraph.checkpoint.memory import InMemorySaver
from deepagents.backends import FilesystemBackend

from app.agent.context import EnhancedSummarizationMiddleware
from app.agent.task_state import TaskStateMiddleware


class Model(FakeMessagesListChatModel):
    def bind_tools(self, tools, **kwargs):
        return self


def test_task_state_survives_compaction_rebuild_and_rejects_stale_updates(tmp_path):
    snapshot = {"goal": "修复接口", "constraints": ["不得改变公开 API"],
                "completed": [{"text": "修复解析器", "source": "tool:test-42 运行通过"}], "pending": ["检查文档"]}
    def call(revision, value):
        return AIMessage(content="", tool_calls=[{"id": f"update-{revision}-{value['goal']}", "name": "update_task_state",
            "args": {"snapshot": value, "expected_revision": revision}}])
    model = Model(responses=[call(0, snapshot), AIMessage(content="已保存"), AIMessage(content="继续"),
                            call(0, {"goal": "过期目标"}), AIMessage(content="版本冲突已保留原状态")])
    saver = InMemorySaver()
    summary = EnhancedSummarizationMiddleware(model, backend=FilesystemBackend(root_dir=tmp_path, virtual_mode=True),
        trigger=("messages", 4), keep=("messages", 2), summarizer=lambda _: "short summary omitting the constraint")
    task = TaskStateMiddleware()
    summary.request_projectors = [task]
    def graph():
        return create_agent(model, middleware=[summary, task], checkpointer=saver)
    async def run():
        config = {"configurable": {"thread_id": "task-state"}}
        agent = graph()
        await agent.ainvoke({"messages": [HumanMessage(content="修复接口，不能改变 API")]}, config)
        await agent.ainvoke({"messages": [HumanMessage(content="先检查文档")]}, config)
        agent = graph()
        state = await agent.aget_state(config)
        assert state.values["task_state"]["revision"] == 1
        assert state.values["_summarization_event"]
        from langchain.agents.middleware.types import ModelRequest
        prompt = task.modify_request(ModelRequest(model=model, messages=[], state=state.values)).system_message.text
        assert "不得改变公开 API" in prompt and "先检查文档" in prompt and "tool:test-42" in prompt
        await agent.ainvoke({"messages": [HumanMessage(content="继续执行")]}, config)
        state = await agent.aget_state(config)
        assert state.values["task_state"]["goal"] == "修复接口"
        assert any(getattr(m, "status", None) == "error" and "版本冲突" in m.content for m in state.values["messages"])
    asyncio.run(run())


def test_user_corrections_do_not_rotate_back_to_older_messages():
    task = TaskStateMiddleware()
    messages = [HumanMessage(content=f"correction {i}", id=str(i)) for i in range(12)]
    async def run():
        state = {"messages": messages}
        state.update(await task.abefore_model(state, None))
        assert [m["id"] for m in state["task_user_updates"]] == ["0", "7", "8", "9", "10", "11"]
        assert await task.abefore_model(state, None) is None
    asyncio.run(run())


def test_memory_notice_does_not_become_a_user_instruction_in_system_prompt():
    task = TaskStateMiddleware()
    async def check():
        update = await task.abefore_model({"messages": [HumanMessage(content="actual task", id="user"),
            HumanMessage(content="disk memory change", id="memory", additional_kwargs={"lc_source": "workspace_memory"})]}, None)
        assert update["task_user_updates"] == [{"id": "user", "text": "actual task", "truncated": False}]
    asyncio.run(check())
