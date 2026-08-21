"""Agent 运行时：构建并运行 DeepAgents 智能体。

P1 最小版本：进程内内存会话，无持久化（P2 迁移到 PostgreSQL + checkpointer/store）。
"""
import uuid

from deepagents import create_deep_agent

from app.agent.model import build_llm

# 内存会话存储：session_id -> 消息列表（元素为 ("user"/"assistant", content)）
# TODO(P2): 迁移到 PostgreSQL + langgraph checkpointer/store
_sessions: dict[str, list[tuple[str, str]]] = {}

_agent = None


def get_agent():
    """懒加载：首次调用时才构建智能体（避免 import 时初始化模型）。"""
    global _agent
    if _agent is None:
        _agent = create_deep_agent(
            model=build_llm(),
            system_prompt=(
                "你是一个通用智能体助手，运行在基于 DeepAgents 框架的 Harness 上。"
                "请用中文简洁、准确地回答用户的问题。"
            ),
            name="assistant",
        )
    return _agent


def new_session() -> str:
    """创建新会话，返回 session_id。"""
    session_id = uuid.uuid4().hex
    _sessions[session_id] = []
    return session_id


def has_session(session_id: str) -> bool:
    return session_id in _sessions


async def run_chat(session_id: str, user_message: str) -> str:
    """向智能体发送一条用户消息，返回回复文本，并把本轮对话写入内存历史。"""
    history = _sessions[session_id]
    messages = history + [("user", user_message)]

    result = await get_agent().ainvoke({"messages": messages})

    reply = result["messages"][-1].content
    if not isinstance(reply, str):
        reply = str(reply)

    history.append(("user", user_message))
    history.append(("assistant", reply))
    return reply
