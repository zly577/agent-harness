"""Agent 运行时：构建并运行 DeepAgents 智能体（P2：持久化 + 上下文管理）。

P1：手工 ("role", content) 二元组内存存储。
P2：- 重构为真实 BaseMessage，历史交给 langgraph checkpointer（SQLite）持久化
     - 通过 thread_id 实现多会话隔离
     - 接入 SummarizationMiddleware 做上下文压缩
     - 业务会话元信息落到 MySQL（app.db.models.Session）
"""
import logging
import uuid

from deepagents import create_deep_agent

from app.agent.context import build_summary_middleware, get_checkpointer, get_store
from app.agent.mcp import build_mcp_tools
from app.agent.middleware import AuditMiddleware, LoggingMiddleware, RateLimitMiddleware
from app.agent.model import build_llm
from app.agent.sandbox import build_main_backend
from app.agent.skills import build_skills_middleware, compile_all_skills, seed_skills
from app.agent.subagents import load_all_subagents
from app.core.config import settings
from app.db.models import Session as DbSession
from app.db.session import SessionLocal

logger = logging.getLogger(__name__)

_agent = None


async def get_agent():
    """懒加载：首次调用时才构建智能体（异步构建 checkpointer/store/backend）。"""
    global _agent
    if _agent is None:
        backend = build_main_backend()

        # P4: 编译 YAML 技能定义并播种进 backend 虚拟文件系统
        compile_all_skills()
        await seed_skills(backend)

        # P5: 中间件管道 = 上下文压缩 + 技能 + 日志 + 审计 + 限流（默认关闭）
        middlewares = [
            build_summary_middleware(),
            LoggingMiddleware(),
            AuditMiddleware(),
            RateLimitMiddleware(max_messages=0),
        ]
        skills_mw = build_skills_middleware(backend)

        # P7: 外部 MCP 工具（MCP_ENABLED=1 + 装依赖 + 网络可达时才加载）
        extra_tools: list = []
        if settings.mcp_enabled:
            mcp_tools, mcp_errors = await build_mcp_tools()
            extra_tools.extend(mcp_tools)
            for err in mcp_errors:
                logger.warning("[mcp] %s", err)

        _agent = create_deep_agent(
            model=build_llm(),
            system_prompt=(
                "你是一个通用智能体助手，运行在基于 DeepAgents 框架的 Harness 上。"
                "请用中文简洁、准确地回答用户的问题。"
            ),
            name="assistant",
            checkpointer=await get_checkpointer(),
            store=await get_store(),
            backend=backend,
            middleware=middlewares + [skills_mw],
            tools=extra_tools or None,
            # P6: 子智能体（YAML 注册表），同一模型；task 工具委派 + 隔离上下文
            subagents=load_all_subagents(),
        )
    return _agent


def create_session() -> str:
    """创建新会话：
    - 生成 session_id（作为 langgraph 的 thread_id）
    - 在 MySQL 业务库登记会话元信息
    - 返回 session_id
    """
    session_id = uuid.uuid4().hex
    db = SessionLocal()
    try:
        db.add(DbSession(id=session_id, title="新会话", status="active"))
        db.commit()
    finally:
        db.close()
    return session_id


def session_exists(session_id: str) -> bool:
    """判断会话是否存在于业务库。"""
    db = SessionLocal()
    try:
        return db.get(DbSession, session_id) is not None
    finally:
        db.close()


async def run_chat(session_id: str, user_message: str) -> str:
    """向智能体发送一条用户消息，返回回复文本。

    - thread_id = session_id，历史由 checkpointer 管理（无需手工存储消息）
    - 输入直接传 user 消息（由 langgraph 转为 BaseMessage）
    """
    agent = await get_agent()
    result = await agent.ainvoke(
        {"messages": [{"role": "user", "content": user_message}]},
        config={"configurable": {"thread_id": session_id}},
    )
    reply = result["messages"][-1].content
    if not isinstance(reply, str):
        reply = str(reply)
    return reply
