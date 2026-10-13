"""Durable user clarification, independent of operation approval settings."""
from langchain.agents.middleware.types import AgentMiddleware, hook_config
from langchain_core.messages import AIMessage, ToolMessage
from langchain_core.tools import tool
from langgraph.types import interrupt
from pydantic import BaseModel, Field, field_validator

CLARIFICATION_POLICY = """当任务目标存在会影响执行结果的歧义，或缺少无法自行查明的必要信息时，必须调用 ask_user 提问并等待回答。
优先查看上下文或读取相关资料；不要询问已经给出的信息。能合理默认的小细节直接处理并说明假设。
一次问清相关关键信息，问题简短具体，可给出选项，也允许自由回答。
调用 ask_user 的这一轮不要并行调用其他工具或委派子任务；收到回答前不得执行依赖缺失信息的操作。
用户取消澄清后停止该任务，不得猜测答案或继续执行。操作审批与澄清独立，关闭审批不表示不用澄清。
"""

class AskUserInput(BaseModel):
    question: str = Field(min_length=1, max_length=2000, description="需要用户补充的具体问题，可合并相关问题")
    options: list[str] = Field(default_factory=list, max_length=6, description="可选答案，允许用户自由输入")

    @field_validator("question")
    @classmethod
    def valid_question(cls, value):
        if not value.strip():
            raise ValueError("question cannot be blank")
        return value.strip()

    @field_validator("options")
    @classmethod
    def valid_options(cls, values):
        if any(not value.strip() or len(value) > 300 for value in values):
            raise ValueError("options must be nonblank and at most 300 characters")
        return list(dict.fromkeys(value.strip() for value in values))

@tool("ask_user", args_schema=AskUserInput)
def ask_user(question: str, options: list[str] | None = None) -> str:
    """任务有关键歧义或缺少必要信息时，提问并暂停，等待用户的答案。

    不要与其他工具并行调用。用户可以选择选项或自由回答。
    """
    response = interrupt({"kind": "clarification", "question": question, "options": options or []})
    if response.get("cancelled"):
        return CANCELLED_REPLY
    return str(response["answer"])


CANCELLED_REPLY = "用户已取消当前任务。停止执行，不要猜测答案或继续此任务。"

class ClarificationMiddleware(AgentMiddleware):
    """Ask first, and terminate cancelled tasks before another model/tool call."""
    @hook_config(can_jump_to=["end"])
    def before_model(self, state, runtime):
        messages = state.get("messages") or []
        for message in reversed(messages):
            if isinstance(message, AIMessage):
                break
            if isinstance(message, ToolMessage) and (
                (message.name == "ask_user" and message.content == CANCELLED_REPLY)
                or (message.name == "task" and message.content == "已取消当前任务。")
            ):
                return {"messages": [AIMessage(content="已取消当前任务。")], "jump_to": "end"}
        return None

    @hook_config(can_jump_to=["end"])
    async def abefore_model(self, state, runtime):
        return self.before_model(state, runtime)

    @staticmethod
    def _prioritize(response):
        from dataclasses import replace
        messages = list(response.result)
        for index, message in enumerate(messages):
            if not isinstance(message, AIMessage):
                continue
            questions = [call for call in message.tool_calls if call["name"] == "ask_user"]
            if questions and len(message.tool_calls) > 1:
                messages[index] = message.model_copy(update={"tool_calls": questions[:1],
                    "additional_kwargs": {key: value for key, value in message.additional_kwargs.items() if key != "tool_calls"}})
        return replace(response, result=messages)

    def wrap_model_call(self, request, handler):
        return self._prioritize(handler(request))

    async def awrap_model_call(self, request, handler):
        return self._prioritize(await handler(request))
