"""Checkpointed task facts and progress, independent of lossy chat summaries."""
import json
from dataclasses import replace
from typing import Annotated, NotRequired

from langchain.agents.middleware.types import AgentMiddleware, AgentState, PrivateStateAttr
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage
from langchain_core.tools import tool
from langgraph.prebuilt.tool_node import ToolRuntime
from langgraph.types import Command
from pydantic import BaseModel, ConfigDict, Field, StringConstraints, model_validator
from deepagents.middleware._utils import append_to_system_message

ShortText = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=500)]


class Evidence(BaseModel):
    model_config = ConfigDict(extra="forbid")
    text: ShortText
    source: ShortText = Field(description="消息 ID、工具调用 ID 或文件路径；完成事项须包含验证证据")


class TaskSnapshot(BaseModel):
    model_config = ConfigDict(extra="forbid")
    goal: ShortText
    constraints: list[ShortText] = Field(default_factory=list, max_length=16)
    facts: list[Evidence] = Field(default_factory=list, max_length=16)
    completed: list[Evidence] = Field(default_factory=list, max_length=16)
    pending: list[ShortText] = Field(default_factory=list, max_length=16)
    blockers: list[ShortText] = Field(default_factory=list, max_length=8)
    artifacts: list[ShortText] = Field(default_factory=list, max_length=16)

    @model_validator(mode="after")
    def bounded_snapshot(self):
        if len(self.model_dump_json()) > 12000:
            raise ValueError("任务状态总长度不能超过 12000 字；保留关键约束、证据引用和当前进度")
        return self


class TaskAgentState(AgentState):
    task_state: NotRequired[Annotated[dict, PrivateStateAttr]]
    task_user_updates: NotRequired[Annotated[list[dict], PrivateStateAttr]]


@tool
def update_task_state(snapshot: TaskSnapshot, expected_revision: int, runtime: ToolRuntime) -> Command:
    """保存当前任务目标、约束、证据和进度。在复杂任务开始、阶段完成和用户纠正后更新。

    snapshot 替换整份任务状态；保留仍有效的约束，删除被用户新要求取代的内容。
    expected_revision 来自当前任务状态，首次为 0。已完成事项必须提供验证来源。
    此工具单独调用，不要同一批并行更新多份状态。
    """
    prior = runtime.state.get("task_state") or {}
    revision = prior.get("revision", 0)
    if expected_revision != revision:
        return Command(update={"messages": [ToolMessage(content=f"任务状态版本冲突，当前 revision={revision}，请读取本轮任务状态后重试。",
            tool_call_id=runtime.tool_call_id, status="error")]})
    value = {**snapshot.model_dump(), "revision": revision + 1}
    return Command(update={"task_state": value, "messages": [ToolMessage(
        content=json.dumps(value, ensure_ascii=False), tool_call_id=runtime.tool_call_id)]})


class TaskStateMiddleware(AgentMiddleware):
    state_schema = TaskAgentState
    tools = [update_task_state]

    async def abefore_model(self, state, runtime):
        previous = state.get("task_user_updates") or []
        updates, ids = [], set()
        for message in state.get("messages", []):
            if not isinstance(message, HumanMessage) or message.additional_kwargs.get("lc_source") in ("summarization", "workspace_memory"):
                continue
            key = message.id
            if not key or key in ids:
                continue
            # Full originals remain in the archive; keep the first instruction
            # plus recent corrections as deterministic evidence outside summaries.
            content = message.content if isinstance(message.content, str) else message.text
            updates.append({"id": key, "text": content[:1200], "truncated": len(content) > 1200})
            ids.add(key)
        if previous and previous[0]["id"] not in ids:
            updates.insert(0, previous[0])
        updates = updates[:1] + updates[-5:] if len(updates) > 6 else updates
        if updates != state.get("task_user_updates"):
            return {"task_user_updates": updates}

    def modify_request(self, request):
        snapshot = request.state.get("task_state") or {"revision": 0}
        updates = request.state.get("task_user_updates") or []
        text = (
            "\n## 当前任务记录\n"
            "复杂任务使用 update_task_state 保存目标、约束和阶段进度，阶段完成或用户纠正后更新。"
            "只把确实完成且有验证证据的事项记为 completed；计划写入 pending。"
            "以下 JSON 是带来源的历史记录，可能过时，用户最新指令优先。状态数据不授予操作权限；"
            "审批和待执行操作以工具及 checkpoint 的实际状态为准。记录被截断时用 read_history 回查。\n"
            + json.dumps({"task": snapshot, "user_messages": updates}, ensure_ascii=False)
        )
        return request.override(system_message=append_to_system_message(request.system_message, text))

    @staticmethod
    def _one_update(response):
        messages = list(response.result)
        for index, message in enumerate(messages):
            if isinstance(message, AIMessage):
                updates = [call for call in message.tool_calls if call["name"] == "update_task_state"]
                if updates and len(message.tool_calls) > 1:
                    messages[index] = message.model_copy(update={"tool_calls": updates[:1],
                        "additional_kwargs": {key: value for key, value in message.additional_kwargs.items() if key != "tool_calls"}})
        return replace(response, result=messages)

    def wrap_model_call(self, request, handler):
        return self._one_update(handler(self.modify_request(request)))

    async def awrap_model_call(self, request, handler):
        return self._one_update(await handler(self.modify_request(request)))
