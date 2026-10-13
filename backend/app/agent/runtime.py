"""Agent 运行时：构建并运行 DeepAgents 智能体（持久化 + 上下文管理）。

手工 ("role", content) 二元组内存存储。
重构为真实 BaseMessage，历史交给 langgraph checkpointer（SQLite）持久化
     - 通过 thread_id 实现多会话隔离
     - 接入 SummarizationMiddleware 做上下文压缩
     - 业务会话元信息落到 MySQL（app.db.models.Session）
"""
import asyncio
import json
import logging
import os
import time
import uuid
from deepagents import create_deep_agent
from langchain_core.messages import ToolMessage
from langchain_core.tools import tool
from langchain_tavily import TavilySearch
from pydantic import BaseModel, Field
from sqlalchemy import func

from app.agent.context import build_summary_middleware, get_checkpointer, get_store
from app.agent.intent import resolve_intent, strip_intent
from app.agent import access
from app.agent.mcp import build_mcp_tools
from app.agent.mcp_registry import load_mcp_configs
from app.agent.middleware import (
    AuditMiddleware,
    LoggingMiddleware,
    build_failure_limit_middlewares,
    build_intent_middlewares,
    build_self_kill_guard_middleware,
    build_tool_call_limit_middlewares,
)
from app.agent.model import build_llm
from app.agent.permissions import agent_permissions, hitl_interrupt_on
from app.agent.prompt import build_system_prompt, memory_sources
from app.agent.sandbox import build_main_backend, validate_permissions
from app.agent.skills import build_skills_middleware, compile_all_skills, seed_skills
from app.agent.subagent_stream import (
    SubagentCallRegistry,
    apply_subagent_timeout,
    iter_subagent_events,
    log_usage_event,
    normalize_chunk,
    parse_subagent_namespace,
    reasoning_delta,
    streaming_astream,
    summarize_task,
)
from app.agent.subagents import load_all_subagents
from app.agent.tool_outcome import classify_tool_result
from app.core.config import settings
from app.core.workspace import workspace_for
from app.db.models import Session as DbSession
from app.db.session import SessionLocal
from langchain.agents.middleware.todo import TodoListMiddleware
logger = logging.getLogger(__name__)

#: 会话 → (agent, backend)。每个会话一个 agent（因为它绑定一个沙箱容器）+ 一份本地磁盘视图。
_agents: dict[str, tuple[object, object]] = {}
#: 会话 → 归属用户。只用于"按用户批量失效 agent 缓存"（见 invalidate_agents）。
#: 不引入这个映射的话，改某个用户的 HITL 默认值时只能清空全部 —— 那会把
#: 其他用户正在跑的会话的 agent 一起重建（不危险，但纯属无谓的代价）。
_agent_owner: dict[str, str | None] = {}
_MAIN_SESSION = "default"


def session_context(session_key: str) -> tuple[str | None, str | None]:
    """一次查询拿到会话的两个关键属性：`(归属用户 id, 绑定的供应商 id)`。

    合成一个函数是为了避免"每构建一次 agent 就查两遍库"——更实际的理由是
    这两件事的**用途紧邻**（决定工作区 + 决定模型），分开写容易出现
    "在 A 处查归属、在 B 处查供应商"那种散开的代码。
    """
    if not session_key:
        return None, None
    db = SessionLocal()
    try:
        row = db.get(DbSession, session_key)
        if row is None:
            return None, None
        return (row.user_id or None), (row.provider_id or None)
    finally:
        db.close()


def session_owner(session_id: str) -> str | None:
    """会话的归属用户 id（查业务库；不存在或未归属时返回 None）。

    ## 为什么要有这个"查库兜底"

    工作区与长期记忆都按用户隔离，于是 `get_agent()` 必须知道"这是谁的工作区"。
    最省事的做法是让每个调用点都把 `user_id` 传下来 —— 但只要**有一处忘了**，
    那个调用点就会落到匿名目录，表现是"我刚写的文件不见了"（写到了另一个目录），
    是那种很难一眼看出的 bug。

    所以 `get_agent()` / `get_main_backend()` 在 `user_id` 为空时会来这里查一次。
    路径上多一次主键查询（agent 只在缓存未命中时才构建），换"忘记传参也不会串目录"。
    """
    return session_context(session_id)[0]


def _resolve_provider_for_agent(user_id: str | None, session_provider_id: str | None):
    """解析本次要用的供应商配置（查库 + 解密）。

    单独抽出来是为了让单元测试能直接替换它（起一个真实 agent 太重），
    以及让"解析失败"这件事在 get_agent 里只有一处出口。
    """
    from app.agent.providers import resolve_for_agent

    db = SessionLocal()
    try:
        return resolve_for_agent(db, user_id, session_provider_id)
    finally:
        db.close()


def set_session_provider(session_id: str, provider_id: str | None) -> bool:
    """把会话的模型选择写回库，并让它的 agent 缓存失效。

    ## 必须重建 agent 的原因

    模型是 `create_deep_agent(model=...)` 的**编译期输入**，缓存里的那个 graph
    绑死了构建时的模型实例。只改数据库不会让正在跑/待跑的会话换模型 ——
    表现是"我明明换了供应商，它还在用旧的"，而且换回来也一样（因为缓存没动）。
    所以"改 provider"和"重建 agent"必须成对出现，这就是把它们放进同一个函数的原因。

    Returns:
        是否真的发生了变更（没变就不重建，避免白白丢掉沙箱容器绑定）。
    """
    if not session_id:
        return False
    new_value = (provider_id or "").strip() or None
    db = SessionLocal()
    try:
        row = db.get(DbSession, session_id)
        if row is None:
            return False
        if (row.provider_id or None) == new_value:
            return False
        row.provider_id = new_value
        db.commit()
    finally:
        db.close()
    invalidate_agents(session_id)
    logger.info("[runtime] 会话 %s 的模型供应商已切换为 %s", session_id, new_value or "(跟随默认)")
    return True


def session_workspace(session_id: str, owner: str | None):
    """Resolve persisted workspace membership, retaining legacy user roots."""
    with SessionLocal() as db:
        row = db.get(DbSession, session_id)
        workspace_id = row.workspace_id if row and row.user_id == owner else None
        if workspace_id:
            from app.db.models import Workspace
            workspace = db.get(Workspace, workspace_id)
            if workspace and workspace.user_id == owner:
                from pathlib import Path
                return Path(workspace.directory)
    root = workspace_for(owner)
    return root / "workspaces" / workspace_id if workspace_id else root


def get_main_backend(session_key: str = _MAIN_SESSION, user_id: str | None = None):
    """获取某会话的 backend（本地磁盘 + 沙箱执行），供文件浏览/下载 API 使用。

    `user_id` 决定**本地磁盘根**（`<base>/<user_id>/`）。传空时会用
    `session_owner()` 兜底查一次（见该函数的说明）；两者都拿不到才落到匿名目录。

    注意这里刻意**不**按用户全局缓存 backend：沙箱容器是按会话绑定的状态，
    缓存进错会话会串台；文件 API 这条路径只需要"能读写本地磁盘"，
    每次构建的成本只是一组对象（实测 get_agent 冷启动里纯 IO 只占 7ms）。
    """
    session_key = session_key or _MAIN_SESSION
    cached = _agents.get(session_key)
    if cached is not None:
        return cached[1]
    owner = user_id or session_owner(session_key)
    # 尚未跑过对话的会话：先按用户建 backend（本地磁盘目录会立即创建）
    # 这里也要跟随该会话的权限模式 —— 文件浏览 API 用到的 backend
    # 必须与对话用的是同一个（完全访问模式下 default 是 HostShellBackend），
    # 否则"文件面板看到的"和"对话读写的"会来自两个不同的后端对象。
    return build_main_backend(
        session_key=session_key,
        workspace=session_workspace(session_key, owner),
        mode=access.access_mode(session_key, owner),
    )


async def get_agent(session_key: str = _MAIN_SESSION, user_id: str | None = None, *, history_only: bool = False):
    """懒加载：按会话构建智能体（backend 绑定该会话的沙箱容器 + 该用户的工作区）。

    Args:
        session_key: 会话标识（= thread_id）。沙箱容器与本地磁盘视图都按会话隔离，
            所以 agent 也必须按会话缓存（不能全局单例）。
        user_id: 工作区归属者，决定本地磁盘根与长期记忆（`Agent.md`）的位置。
            传空时用 `session_owner()` 兜底（避免"忘了传"导致工作区串台）。
    """
    session_key = session_key or _MAIN_SESSION
    cached = _agents.get(session_key)
    if cached is not None:
        return cached[0]

    # 一次查库拿到「归属用户」与「会话绑定的供应商」。
    # user_id 显式传进来时以调用方为准（路由已经做过归属校验），
    # 为空才用查库兜底 —— 避免"某处忘了传"导致工作区串到匿名目录。
    db_owner, session_provider_id = session_context(session_key)
    owner = user_id or db_owner

    # 按「会话 → 用户默认 → 系统默认」解析出本次要用的模型供应商。
    # 这一步必须在构建 graph **之前**完成：模型是 create_deep_agent 的编译期输入，
    # 换供应商只能重建 agent（见 invalidate_agents）。
    if history_only:
        from app.agent.history_model import HistoryModel
        model = HistoryModel()
    else:
        provider = _resolve_provider_for_agent(owner, session_provider_id)
        model = build_llm(provider)

    # 本地磁盘读写（FilesystemBackend）+ OpenSandbox 执行（execute 工具）
    # 本地磁盘根按用户隔离 → 会话/用户之间的文件与 Agent.md 互不可见
    # 权限模式决定"执行层" —— 默认权限进沙箱，完全访问直接在本机跑。
    #      它同时决定 backend、permissions、interrupt_on 与系统提示词（见下），
    #      所以必须在构建 graph 之前解析出来（和模型一样是编译期输入）。
    mode = access.access_mode(session_key, owner)
    backend = build_main_backend(
        session_key=session_key,
        workspace=session_workspace(session_key, owner),
        mode=mode,
    )

    # 自检：权限路径（主 Agent + 各子智能体）必须都落在 route 前缀内，
    # 否则 deepagents 的 FilesystemMiddleware 会抛 NotImplementedError（且只在有
    # 子智能体时才暴露）。这里提前把问题说清楚，避免用户看到一句框架内部报错。
    problems = validate_permissions(backend, mode=mode)
    if problems:
        raise RuntimeError(
            "文件权限配置与沙箱执行后端不兼容：\n  - " + "\n  - ".join(problems)
        )

    # 编译 YAML 技能定义并播种进 backend 虚拟文件系统
    if not history_only:
        compile_all_skills()
        await seed_skills(backend)

    # 中间件管道 = 上下文压缩 + 技能 + 日志 + 审计 + 工具熔断 + 工具调用次数上限
    # 工具熔断：每个工具独立计数，连续失败达阈值 → 冷却期短路 → 冷却后放一次探测自愈；
    # 同时产出"手动恢复"工具（模型可主动调用它重置本会话熔断）。
    failure_middlewares, recovery_tools = build_failure_limit_middlewares()
    middlewares = [
        # **必须排第一**。中间件的顺序 = 洋葱的顺序（第一个最外层，见
        # langchain `_chain_tool_call_wrappers` 的注释），而这一层的职责是
        # "在执行任何工具之前摘掉 `_intent`"。放里面（比如放到 HITL 之后）会出现
        # "审批弹窗里带着 `_intent`"、甚至"熔断器看到的是没摘干净的参数"。
        *build_intent_middlewares(),
        TodoListMiddleware(),
        # 摘要中间件必须与主模型**同一个实例**：
        # 否则会出现"对话走用户选的供应商、压缩摘要却偷偷走另一个模型"，
        # 而且那条路径在界面上完全不可见（用户只会觉得"摘要质量怎么变了"）。
        build_summary_middleware(model),
        LoggingMiddleware(),
        AuditMiddleware(),
        # 硬护栏 —— 拦掉"按进程名杀 python"这类会把 harness 后端自己杀掉的
        # 命令。放在日志/审计**之后**（这次尝试要留痕）、熔断之前。
        # 详见 middleware.py 里 `SelfKillGuardMiddleware` 的事故注释。
        build_self_kill_guard_middleware(),
        *failure_middlewares,
        # 库自带的调用次数上限（默认关闭；需要"用完 N 次就停"时打开）
        *build_tool_call_limit_middlewares(),
    ]
    skills_mw = build_skills_middleware(backend)

    # 外部 MCP 工具（MCP_ENABLED=1 才加载）
    # 配置来源 = 该用户数据库里的记录 + configs/mcp/*.yaml（运维预置，对所有人可见）。
    # ⚠️ 必须传 owner：MCP 工具是"能读内网/带密钥"的能力，按用户隔离是硬要求，
    #    传空就等于只加载 YAML 那份（老行为），用户自己配的一条都不会出现。
    extra_tools: list = []
    if settings.mcp_enabled and not history_only:
        mcp_tools, mcp_errors = await build_mcp_tools(load_mcp_configs(owner))
        extra_tools.extend(mcp_tools)
        for err in mcp_errors:
            logger.warning("[mcp] %s", err)

    class TavilySearchInput(BaseModel):
        query: str = Field(description="用户的搜索查询词")
        max_results: int = Field(
            description="用户要求返回的搜索结果条数，从用户问题中提取。如果用户没有指定，默认5"
        )

    @tool("tavily_search", args_schema=TavilySearchInput)
    async def tavily_search(query: str, max_results: int = 5):
        """
        使用Tavily联网搜索最新网页信息。
        用户经常会指定需要返回多少条结果，请从用户提问中提取数字填入max_results。
        例如用户说：帮我搜3条关于xxx的资料，max_results就设为3。
        当用户未指定条数，默认max_results=5。

        Args:
            query: 搜索关键词
            max_results: 需要返回的搜索结果数量
        """
        # 缺 key 时必须**快速失败并说清楚**：langchain_tavily 在构造
        # TavilySearchAPIWrapper 时就会抛 ValidationError，且它真正读取的来源是
        # 环境变量 TAVILY_API_KEY（不是 settings）。以前这里只有一行被丢弃的
        # `os.getenv(...)`，等于什么都没配置 —— 结果是子智能体一搜就失败，
        # 它只能白跑几十个模型步才发现"搜不了"，最终报告里写着"搜索工具不可用"。
        #
        # 2026-09-17：返回值必须以 `Error:` 开头。熔断器（middleware._is_tool_failure）
        # 只认三种失败形状：ToolMessage.status=="error" / dict.error / 文本以 "Error:" 开头。
        # 之前这里返回的是"联网搜索不可用：…"这样的**普通文本**，熔断器看不见失败，
        # 于是不计数、不冷却，模型把它当成"这次没搜到"并继续换词重试。
        # 实测一次调研里子智能体 5 个模型步中有 4 步在打搜索，共发出 16 次调用。
        api_key = (settings.tavily_api_key or "").strip() or (os.environ.get("TAVILY_API_KEY") or "").strip()
        if not api_key:
            return (
                "Error: 联网搜索不可用 —— 缺少 Tavily API Key。"
                "请在 backend/.env 配置 TAVILY_API_KEY=...（到 https://tavily.com 申请）后重启后端。"
                "在配置好之前不要依赖联网搜索：改用其他信息来源，或在回答中如实说明无法联网。"
            )
        os.environ["TAVILY_API_KEY"] = api_key

        # 底层调用 Tavily。
        # 必须用 ainvoke：本工具在 agent.astream() 的事件循环里执行，
        # 一旦调用同步的 invoke()，网络往返期间会**阻塞整个 event loop** ——
        # 表现是所有会话的 SSE 一起卡住、前端一个字都刷不出来、审批窗点不动。
        tavily_tool = TavilySearch(
            max_results=max_results,
            search_depth="basic"
        )
        try:
            # 执行超时兜底：Tavily 也可能"连上了却迟迟不返回"，没有上限会像
            # MCP 工具一样把 agent 的 tools 节点卡死。复用同一套超时配置。
            timeout = settings.mcp_tool_call_timeout_seconds
            if timeout and timeout > 0:
                result = await asyncio.wait_for(tavily_tool.ainvoke({"query": query}), timeout=timeout)
            else:
                result = await tavily_tool.ainvoke({"query": query})
            return result
        except asyncio.TimeoutError:
            logger.warning("[tavily] 搜索超时（>%g 秒）", timeout)
            return (
                f"Error: 联网搜索超时（超过 {timeout:g} 秒无响应）。"
                "请改用其他信息来源，或如实说明无法联网，不要重复调用本工具。"
            )
        except Exception as e:
            # 把原因说清楚，模型才知道该换策略，而不是反复重试同一个搜索。
            # 同样必须以 `Error:` 开头 —— 否则熔断器识别不到失败，模型会一直重试。
            logger.warning("[tavily] 搜索失败: %s: %s", type(e).__name__, e)
            return (
                f"Error: 联网搜索失败（{type(e).__name__}）：{e}。"
                "请改用其他信息来源，或如实说明无法联网，不要重复调用本工具。"
            )

    # 注意：不要写成 `tools = extra_tools` —— 那是 MCP 返回的**同一个列表对象**，
    # append 会污染它（多次调用 get_agent 会重复累加）。这里显式拷贝。
    tools = list(extra_tools)
    tools.append(tavily_search)
    # 工具熔断的"手动恢复"入口（模型可主动调用，见 middleware.ConsecutiveToolFailureMiddleware）
    tools.extend(recovery_tools)

    agent = create_deep_agent(
        # 模型来自「会话 → 用户默认 → 系统默认」解析出的供应商配置。
        # 子智能体（subagents）**不传 model**，因此天然继承这一个实例 ——
        # 所以"主智能体、子智能体、摘要中间件"三者走的一定是同一个模型。
        model=model,
        # 框架级规则（稳定）：工具用法 / 委派规范 / 路径约定
        # 完全访问模式下这一段会换成"本机 shell"版本（路径怎么写、
        #      execute 不再进容器），见 prompt.full_access_prompt_section()。
        system_prompt=build_system_prompt(mode=mode),
        name="assistant",
        checkpointer=await get_checkpointer(),
        store=await get_store(),
        backend=backend,
        middleware=middlewares + [skills_mw],
        tools=tools,
        # 长期记忆：Agent.md 每次运行开始时被读入并注入系统提示词，
        # 智能体可用 edit_file 自行更新它（记录用户偏好等），跨会话生效。
        # 按用户隔离 —— 每个人读写的都是自己工作区里的那份。
        memory=memory_sources(owner),
        # 访问本机真实文件 → 人工审批中断（deepagents HITL）
        # 热插拔：开 = 返回 interrupt 权限（访问 /mnt/** 需人工同意）；关 = 空列表。
        # 完全访问模式下一律返回空列表（用户已显式选择"不问了"）。
        # 权限是编译期输入，所以模式/开关变化时必须丢掉这个缓存的 agent 重建。
        # 开关按「会话 → 用户 → 全局」三级解析，所以这里要把 owner 传下去，
        #      否则某个用户关掉审批后，他自己的会话依然会弹窗。
        permissions=agent_permissions(session_key, owner, mode),
        # 关闭人工介入（或完全访问）时给一个**永不匹配的占位键**，只为让 deepagents
        # 仍然装配 HumanInTheLoopMiddleware —— **图结构在开关前后必须一致**，否则已挂起的
        # 中断会因为"图里没有 HITL 节点"而读不出来（点同意 → 409 无待审批项），
        # 再打开时又冒出来（弹窗复活）。详见 app/agent/permissions.py 模块注释。
        interrupt_on=hitl_interrupt_on(session_key, owner, mode),
        # 子智能体（YAML 注册表），同一模型；task 工具委派 + 隔离上下文
        subagents=load_all_subagents(),
    )
    if history_only:
        # Only aget_state may use this graph. Never cache it as an executable
        # agent: sending a message must still initialize the real model/tools.
        return agent

    # 给内建的 task 工具加超时上限：deepagents 的 task 是 `await subagent.ainvoke(...)`，
    # 期间没有任何事件产出，不设限就只能看着转圈（见 subagent_stream 模块头注释）。
    # 注意：返回值必须当真 —— 返回 False 说明**这轮没有超时保护**（例如 langgraph
    # 换了工具存放位置），必须留痕，不能让"有超时"变成想当然。
    if not apply_subagent_timeout(agent):
        logger.warning(
            "[runtime] task 工具未套上超时（%s 秒的配置不生效）："
            "子智能体若卡住将无限等待。",
            settings.subagent_timeout_seconds,
        )
    _agents[session_key] = (agent, backend)
    _agent_owner[session_key] = owner
    return agent


def invalidate_agents(session_key: str | None = None, user_id: str | None = None) -> list[str]:
    """丢掉已缓存的 Agent（权限/配置/模型变了必须重建 graph 才会生效）。

    Args:
        session_key: 只丢该会话。
        user_id: 只丢该用户的会话。用于"某个用户改了 HITL 默认值"这类
            只影响他本人的场景 —— 没必要把别人的 agent 也重建一遍。
        （两个都不传 = 丢全部，保持原有语义。）

    Returns:
        被丢弃的会话键列表。
    """
    if session_key:
        dropped = [session_key] if _agents.pop(session_key, None) is not None else []
        _agent_owner.pop(session_key, None)
    elif user_id is not None:
        dropped = [k for k, owner in _agent_owner.items() if owner == user_id]
        for k in dropped:
            _agents.pop(k, None)
            _agent_owner.pop(k, None)
    else:
        dropped = list(_agents.keys())
        _agents.clear()
        _agent_owner.clear()
    if dropped:
        logger.info("[runtime] agent 缓存已失效，下次对话重建: %s", dropped)
    return dropped


def session_title(message: str) -> str:
    """从首条用户消息生成会话标题（压缩空白并截断）。"""
    text = " ".join((message or "").strip().split())
    if not text:
        return "新会话"
    return text[:30] + "…" if len(text) > 30 else text


def create_session(user_id: str, title: str | None = None, workspace_id: str | None = None) -> str:
    """创建新会话：
    - 生成 session_id（作为 langgraph 的 thread_id）
    - 在 MySQL 业务库登记会话元信息（**绑定归属用户**）
    - 返回 session_id

    `user_id` 是必填。会话的归属是全部隔离判定的根 ——
    允许它为空会立刻打开一个"谁都看不到"的缺口（或者是更糟的"谁都能看到"）。
    """
    session_id = uuid.uuid4().hex
    db = SessionLocal()
    try:
        db.add(DbSession(id=session_id, user_id=user_id, workspace_id=workspace_id, title=title or "新会话", status="active"))
        db.commit()
    finally:
        db.close()
    return session_id


def session_exists(session_id: str) -> bool:
    """判断会话是否存在于业务库。

    ⚠️ 这个函数**不做归属校验**，不要把它当成鉴权用（那会让"别人的会话"也返回 True）。
    路由层的归属校验统一走 `app/api/deps.py::owned_session_or_404`。
    """
    db = SessionLocal()
    try:
        return db.get(DbSession, session_id) is not None
    finally:
        db.close()


def touch_session(session_id: str) -> None:
    """继续对话时刷新会话 updated_at，使会话在列表中置顶。"""
    db = SessionLocal()
    try:
        db.query(DbSession).filter(DbSession.id == session_id).update(
            {"updated_at": func.now()}
        )
        db.commit()
    finally:
        db.close()


def _extract_text(content) -> str:
    """把 BaseMessage.content（str 或 list）归一为纯文本。"""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for p in content:
            if isinstance(p, dict):
                parts.append(str(p.get("text", "") or ""))
            else:
                parts.append(str(p))
        return "".join(parts)
    return str(content or "")


def _resolve_intent(tool_name: str, args) -> tuple[dict, str | None, str | None]:
    """统一的"摘 `_intent` + 算展示标签"入口。

    Returns:
        `(干净参数, intent_label, intent_source)`。
        功能关闭时标签为 `(None, None)` —— 看板回落到自己原来的展示，
        但**参数照样要摘干净**（这是安全/正确性不变量，不是展示偏好）。
    """
    if not getattr(settings, "intent_label_enabled", True):
        clean, _ = strip_intent(args)
        return clean, None, None
    return resolve_intent(tool_name, args)


def _log_tool_event(session_id: str, event: str, call_id: str, name: str,
                    args: dict | None = None, result: str = "",
                    user_id: str | None = None,
                    intent_label: str | None = None,
                    intent_source: str | None = None,
                    status: str = "done") -> None:
    """把工具调用事件即时持久化到 MySQL tool_call_logs（失败只告警，不影响流式主流程）。

    - event="call"：插入一条 running 记录
    - event="result"：按 call_id 回填结果并置 `status`

    `status` 允许是 `done` / `error`。以前无论成败一律写 `done`，于是
    "哪一步失败了"在库里根本查不出来（前端也只能显示"已完成"），这是本次
    要解决的问题之一。写 `error` 同时也让熔断/看板拿到同一份事实。

    写入时一并落 `user_id`。**回填（result）不碰 user_id** ——
    那一列属于"这条记录归谁"，而回填是按 call_id 找既有行的结果更新，
    顺手改写归属只会引入风险而没有收益。

    `call` 事件带上 `intent_label` / `intent_source`，并**同步投影**到
    任务看板的读模型（`task_steps`）。投影和事件写在同一个事务里提交：
    看板读到"工具在跑"但事件存储里没有它，是那种最难查的不一致。

    ⚠️ `args` 必须是**已经摘掉 `_intent` 的干净参数**（调用方用
    `_resolve_intent()` 拿到）。这里不替调用方兜底 —— 静默清洗会让
    "某个调用点忘了摘"永远不被发现。
    """
    from app.db.models import ToolCallLog
    from app.db.projections import project_tool_call

    kind = "task" if name == "task" else "tool"
    db = SessionLocal()
    try:
        if event == "call":
            owner = user_id or session_owner(session_id)
            db.add(ToolCallLog(
                session_id=session_id, user_id=owner,
                call_id=str(call_id or ""), name=str(name or ""),
                kind=kind, args=json.dumps(args or {}, ensure_ascii=False), status="running",
                intent_label=intent_label, intent_source=intent_source,
            ))
            if call_id:
                project_tool_call(
                    db, session_id=session_id, user_id=owner, call_id=str(call_id),
                    tool_name=str(name or ""), kind=kind, status="running",
                    intent_label=intent_label, intent_source=intent_source,
                )
        else:
            row = (db.query(ToolCallLog)
                   .filter(ToolCallLog.session_id == session_id,
                           ToolCallLog.call_id == str(call_id or ""))
                   .order_by(ToolCallLog.id.desc()).first())
            if row is not None:
                row.result = str(result or "")
                row.status = status
            else:
                # 结果先于调用记录到达（罕见），补一条完整记录保证不丢
                db.add(ToolCallLog(
                    session_id=session_id, user_id=user_id or session_owner(session_id),
                    call_id=str(call_id or ""), name=str(name or ""),
                    kind=kind, args="{}", result=str(result or ""), status=status,
                ))
            if call_id:
                project_tool_call(
                    db, session_id=session_id, call_id=str(call_id),
                    tool_name=str(name or ""), kind=kind, status=status,
                )
        db.commit()
    except Exception as e:
        logger.warning("[runtime] tool log write failed: %s", e)
        db.rollback()
    finally:
        db.close()


def _load_tool_log(session_id: str) -> list[dict]:
    """读取会话的工具调用流水（按发生顺序）。表尚不存在等异常时返回空列表。

      说明：这里**只按 session_id 过滤，不加 user_id**。
    理由是 session_id 是不可猜的 uuid hex，而上游路由已经做过归属校验；
    更要紧的是存量数据里有几十条 user_id IS NULL 的孤儿流水（它们的会话已被删除），
    再加一层 `user_id = <我>` 会让管理员自己也看不到这些历史记录 ——
    为了保护一个不存在的攻击面而牺牲可用性，不划算。
    """
    from app.db.models import ToolCallLog

    db = SessionLocal()
    try:
        rows = (db.query(ToolCallLog)
                .filter(ToolCallLog.session_id == session_id)
                .order_by(ToolCallLog.id.asc()).all())
    except Exception as e:
        logger.warning("[runtime] tool log read failed: %s", e)
        return []
    finally:
        db.close()
    out: list[dict] = []
    for r in rows:
        try:
            args = json.loads(r.args) if r.args else {}
        except Exception:
            args = {}
        out.append({
            "id": r.call_id or "", "name": r.name or "", "kind": r.kind or "tool",
            "args": args if isinstance(args, dict) else {},
            "result": r.result or "", "status": r.status or "done",
            # 标签与来源一起带出来（回放/兜底重建都要用它们）
            "intent_label": r.intent_label, "intent_source": r.intent_source,
        })
    return out


def _merge_tool_log(history: list[dict], log_rows: list[dict]) -> list[dict]:
    """把持久化工具流水合并进回放历史（原地修改并返回）。

    - 补齐：回放出的工具调用若结果为空（如服务器在工具执行中途被关闭），
      用日志中的结果回填；
    - 兜底：回放完全没有工具痕迹（如 checkpoint 老消息被上下文压缩裁剪），
      用日志重建一条带完整工具调用面板的助手消息。

    同时补齐/带回 `intentLabel` / `intentSource`。**回放必须能确定性还原**，
    所以这里只搬运**已经落库的**标签，绝不重新推导 —— 重新推导会随着
    目录/规则变化而漂移，那就不是"重放同一条事件得到同一个标签"了。

    还要带上 `status`（`interrupted` / `error`）与 `ok`/`exitCode`/`reason`。
    以前回放一律按 `done` 渲染 —— 那一轮被强杀的工具，刷新页面后就"变成成功了"，
    这正是"明明没跑完却显示已完成/进行中"的来源。
    """
    if not log_rows:
        return history
    by_id = {r["id"]: r for r in log_rows if r["id"]}

    def _apply(tc: dict, lr: dict) -> None:
        """把一条流水搬回一个工具条目（toolCalls 与 delegations 共用）。"""
        if not tc.get("result"):
            tc["result"] = lr["result"]
        if not tc.get("intentLabel") and lr.get("intent_label"):
            tc["intentLabel"] = lr["intent_label"]
            tc["intentSource"] = lr.get("intent_source")
        # 流水里的状态比 checkpoint 更权威：checkpoint 只有"调用过"这件事，
        # 而结果是被强杀/中断的，checkpoint 里根本没有痕迹。
        row_status = str(lr.get("status") or "")
        if row_status == "interrupted":
            # ⚠️ 中断**不能**走下面的成败判定：中断时我们会写一句占位说明
            # （"（上一轮运行被中断，未返回结果）"），那是非空文本，
            # 交给分类器会得出"成功" —— 那就等于把"没跑完"伪装成"跑完了"。
            tc["status"] = "interrupted"
            tc["ok"] = None
            tc["exitCode"] = None
            tc["reason"] = "本轮被中断，未收到结果"
            return
        if row_status == "error":
            tc["status"] = "error"
        # 成败判定走同一个纯函数（实时/回放必须一致）
        outcome = classify_tool_result(str(tc.get("name") or ""), tc.get("result"))
        # 老数据里 `status` 一律是 done（不分成败），所以**判定结果说了算**：
        # 只要判定为失败就标 error。否则会出现"结果写着 Error、徽标却说已完成"
        # 这种自相矛盾的卡片 —— 那比不显示更让人困惑。
        if outcome["ok"] is False:
            tc["status"] = "error"
        tc["ok"] = outcome["ok"]
        tc["exitCode"] = outcome["exit_code"]
        tc["reason"] = outcome["reason"]

    # 1) 按 call_id 补齐缺失的结果与意图标签
    for row in history:
        if row.get("role") != "assistant":
            continue
        for tc in row.get("toolCalls") or []:
            lr = by_id.get(str(tc.get("id", "")))
            if lr:
                _apply(tc, lr)
        for d in row.get("delegations") or []:
            lr = by_id.get(str(d.get("id", "")))
            if lr:
                _apply(d, lr)

    # 2) 兜底重建
    has_tools = any(
        (row.get("toolCalls") or row.get("delegations"))
        for row in history if row.get("role") == "assistant"
    )
    if not has_tools:
        tool_calls = [
            {"id": r["id"], "name": r["name"], "args": r["args"],
             "result": r["result"],
             "status": (r.get("status") if r.get("status") in ("interrupted", "error") else "done"),
             "intentLabel": r.get("intent_label"), "intentSource": r.get("intent_source")}
            for r in log_rows if r["kind"] == "tool"
        ]
        delegations = [
            {"id": r["id"],
             "subagent_type": (r["args"] or {}).get("subagent_type", ""),
             "description": (r["args"] or {}).get("description", ""),
             "result": r["result"],
             "status": (r.get("status") if r.get("status") in ("interrupted", "error") else "done"),
             "intentLabel": r.get("intent_label"), "intentSource": r.get("intent_source")}
            for r in log_rows if r["kind"] == "task"
        ]
        if tool_calls or delegations:
            history.append({
                "role": "assistant", "content": "",
                "toolCalls": tool_calls, "delegations": delegations,
            })
    # 兜底/补齐出来的条目也要有成败判定（否则前端又是清一色"已完成"）
    for row in history:
        if row.get("role") != "assistant":
            continue
        for tc in (row.get("toolCalls") or []) + (row.get("delegations") or []):
            if "ok" not in tc:
                outcome = classify_tool_result(str(tc.get("name") or ""), tc.get("result"))
                tc["ok"] = outcome["ok"]
                tc["exitCode"] = outcome["exit_code"]
                tc["reason"] = outcome["reason"]
    return history


async def load_history(session_id: str, user_id: str | None = None) -> list[dict]:
    """读取会话消息历史，返回 [{role, content, toolCalls, delegations}]（供前端恢复对话）。

    按「轮次」折叠：每条 HumanMessage 记一轮用户消息；取该轮到下一条
    HumanMessage 之间最后一条有文本的 AIMessage 作为本轮助手回复。
    同时收集该轮内的所有工具调用（含 task 子智能体委派）及对应结果，
    使前端回放时能还原工具调用面板和任务清单。

    ⚠️ 本函数**不做归属校验**（它只认 session_id）。调用方必须先过
    `app/api/deps.py::owned_session_or_404`。
    """
    from langchain_core.messages import AIMessage, HumanMessage, ToolMessage

    # **刷新页面**才是用户遇到"卡在进行中"之后最自然的动作，而以前只有
    # "发下一条消息"才会收尾遗留的 `running` 流水 ⇒ 光刷新看到的还是旧假象
    # （徽标说"进行中"，而那一轮其实早就没了）。这里补上这条路径。
    _reap_stale_if_idle(session_id)

    agent = await get_agent(session_id, user_id=user_id, history_only=True)
    state = await agent.aget_state({"configurable": {"thread_id": session_id}})
    msgs = (state.values or {}).get("messages") or []

    result: list[dict] = []
    i = 0
    n = len(msgs)
    while i < n:
        m = msgs[i]
        if isinstance(m, HumanMessage):
            user_text = _extract_text(getattr(m, "content", ""))
            assistant_text = ""
            tool_calls: list[dict] = []
            delegations: list[dict] = []
            tool_results: dict[str, str] = {}

            j = i + 1
            while j < n and not isinstance(msgs[j], HumanMessage):
                msg = msgs[j]
                if isinstance(msg, AIMessage):
                    t = _extract_text(getattr(msg, "content", ""))
                    if t.strip():
                        assistant_text = t
                    # 收集该 AIMessage 中的工具调用
                    for tc in getattr(msg, "tool_calls", None) or []:
                        if not isinstance(tc, dict):
                            continue
                        tc_id = str(tc.get("id", ""))
                        tc_name = str(tc.get("name", ""))
                        tc_args = tc.get("args") or {}
                        # checkpoint 里存的是**模型原始输出**（args 里带
                        # `_intent`）。回放给前端的那份参数必须摘干净，标签则
                        # 用同一个纯函数重算 —— 同一份 args 一定得到同一句话，
                        # 所以"重放"与"实时"看到的一致（`_merge_tool_log` 只在
                        # 重算不出标签时，才用库里落的那份兜底）。
                        clean_args, intent_label, intent_source = _resolve_intent(tc_name, tc_args)
                        if tc_name == "task":
                            delegations.append({
                                "id": tc_id,
                                "subagent_type": clean_args.get("subagent_type", ""),
                                "description": clean_args.get("description", ""),
                                "result": "",
                                "status": "done",
                                "intentLabel": intent_label,
                                "intentSource": intent_source,
                            })
                        else:
                            tool_calls.append({
                                "id": tc_id,
                                "name": tc_name,
                                "args": clean_args,
                                "result": "",
                                "status": "done",
                                "intentLabel": intent_label,
                                "intentSource": intent_source,
                            })
                elif isinstance(msg, ToolMessage):
                    tc_id = str(getattr(msg, "tool_call_id", "") or "")
                    tool_results[tc_id] = str(getattr(msg, "content", "") or "")
                j += 1

            # 把工具结果回填到对应调用
            for tc in tool_calls:
                tc["result"] = tool_results.get(tc["id"], "")
            for d in delegations:
                d["result"] = tool_results.get(d["id"], "")

            result.append({"role": "user", "content": user_text})
            result.append({
                "role": "assistant",
                "content": assistant_text,
                "toolCalls": tool_calls,
                "delegations": delegations,
            })
            i = j
        else:
            i += 1

    # 与持久化工具日志合并：补齐中途被关闭导致的结果缺失 / 兜底重建被压缩裁剪的调用
    return _merge_tool_log(result, _load_tool_log(session_id))


async def run_chat(session_id: str, user_message: str, user_id: str | None = None) -> str:
    """向智能体发送一条用户消息，返回回复文本。

    - thread_id = session_id，历史由 checkpointer 管理（无需手工存储消息）
    - 输入直接传 user 消息（由 langgraph 转为 BaseMessage）
    """
    agent = await get_agent(session_id, user_id=user_id)
    result = await agent.ainvoke(
        {"messages": [{"role": "user", "content": user_message}]},
        config={"configurable": {"thread_id": session_id}},
    )
    reply = result["messages"][-1].content
    if not isinstance(reply, str):
        reply = str(reply)
    return reply


def _subagent_start_event(call_id: str, args: dict) -> tuple[str, dict]:
    """生成"子智能体开始"事件：给前端的委派卡片一个**任务摘要**（不是全文）。

    完整 description 仍随 tool_call 事件的 args 下发，前端按需展开；
    这里额外给一份压成一行的摘要，供默认折叠态展示。
    """
    description = str((args or {}).get("description") or "")
    return ("subagent_start", {
        "id": str(call_id or ""),
        "subagent_type": str((args or {}).get("subagent_type") or ""),
        "summary": summarize_task(description),
    })

def mark_stale_tool_calls(session_id: str) -> int:
    """把该会话遗留的 `running` 工具流水收尾成 `interrupted`。

    为什么需要：进程被强杀 / 重启后，那些"已记录调用、结果还没回来"的行会永远停在
    `running`。前端据此渲染的工具卡片就一直是"执行中"，看起来像卡住了，实际那一轮
    早就没了。新一轮开始时统一收尾，语义最贴近事实（那一轮确实被中断了）。

    Returns:
        收尾的行数。
    """
    from app.db.models import ToolCallLog

    db = SessionLocal()
    try:
        rows = (db.query(ToolCallLog)
                .filter(ToolCallLog.session_id == session_id,
                        ToolCallLog.status == "running")
                .all())
        if not rows:
            return 0
        for r in rows:
            r.status = "interrupted"
            if not (r.result or "").strip():
                r.result = "（上一轮运行被中断，未返回结果）"
        # 读模型跟着一起收尾，否则看板上那一步会永远停在"进行中"
        from app.db.projections import project_session

        project_session(db, session_id)
        db.commit()
        logger.info("[runtime] 会话 %s 有 %d 条工具流水遗留在 running，已标记为 interrupted",
                    session_id, len(rows))
        return len(rows)
    except Exception as e:
        logger.warning("[runtime] 收尾遗留 running 流水失败: %s", e)
        db.rollback()
        return 0
    finally:
        db.close()


def _reap_stale_if_idle(session_id: str) -> int:
    """会话**当前没有在跑**时，把遗留的 `running` 工具流水收尾成 `interrupted`。

    为什么要加「没有在跑」这个前提：`mark_stale_tool_calls` 是无条件收尾的，
    它用在"新一轮开始前"是对的（那一刻确实不该有上一轮的遗留）。
    但阅读历史（GET /messages）随时可能发生 —— 用户完全可以在某个长命令
    还在跑的时候刷新页面，那种情况下"正在执行的那一步"本来就是 `running`，
    无条件收尾会把它误标成"已中断"，比不显示更误导。

    拿不准（查询本身失败）时**什么都不做**：宁可多显示一会儿旧状态，
    也不要误标一个正在执行的操作。
    """
    try:
        from app.agent.runner import run_manager

        if run_manager.is_running(session_id):
            return 0
    except Exception as e:  # noqa: BLE001
        logger.debug("[runtime] 查询会话运行态失败，跳过收尾: %s", e)
        return 0
    return mark_stale_tool_calls(session_id)


def _last_assistant_text(messages) -> str:
    """取最后一条**有文本**的 AIMessage 正文。

    ⚠️ 不能只看 `messages[-1].content`：模型决定调工具的那条 AIMessage 的 content
    是空的（只有 tool_calls），而它往往就是最后一条 —— 直接取会得到空字符串，
    表现就是"工具都成功跑完了，却一个字都没回我"。
    """
    from langchain_core.messages import AIMessage

    for m in reversed(list(messages or [])):
        if not isinstance(m, AIMessage):
            continue
        text = _extract_text(getattr(m, "content", ""))
        if text.strip():
            return text
    return ""


async def _final_reply_text(agent, config: dict) -> str:
    """从 checkpoint 里取该会话最后一段真正的助手回复文本。"""
    state = await agent.aget_state(config)
    return _last_assistant_text((state.values or {}).get("messages") or [])


def _empty_reply_fallback(session_id: str) -> str:
    """一轮结束了却没有任何文字时的兜底回复。

    为什么必须有：前端对 `result` 的处理是"有内容才写进气泡"，空字符串等于
    什么都没发生 —— 用户看到的就是"这一轮没有任何回复"。宁可说清楚
    "这轮没有产出文字，但产物在这些位置"，也不要留一个空气泡。
    """
    written: list[str] = []
    read: list[str] = []
    try:
        rows = _load_tool_log(session_id)
    except Exception as e:  # noqa: BLE001 兜底文案不值得让整轮失败
        logger.warning("[runtime] 兜底回复读工具流水失败: %s", e)
        rows = []
    for r in rows[-60:]:
        args = r.get("args") if isinstance(r.get("args"), dict) else {}
        fp = str(args.get("file_path") or args.get("path") or "").strip()
        if not fp:
            continue
        name = str(r.get("name") or "")
        if name in ("write_file", "edit_file"):
            if fp not in written:
                written.append(fp)
        elif name in ("read_file", "ls", "glob", "grep"):
            if fp not in read:
                read.append(fp)

    lines = ["（这一轮结束时没有产出文字回复。）"]
    if written:
        lines.append("最近写入 / 修改过的文件：")
        lines += [f"- `{p}`" for p in written[-8:]]
    if read:
        lines.append("最近查看过：")
        lines += [f"- `{p}`" for p in read[-5:]]
    if not written and not read:
        lines.append("产物位置见左侧文件面板（工作区目录），也可以在面板里下载。")
    return "\n".join(lines)


def args_progress_event(entry: dict, now: float) -> dict | None:
    """工具参数**还在拼装**时的进度心跳；不该发就返回 `None`。

    为什么需要它：模型把大段内容塞进**工具参数**（而不是正文 `content`）时，`tool_call`
    事件必须等参数 JSON 拼完整才发得出来 —— 见下面循环里 `json.loads` 那段。实测一次
    `write_file` 的 `content` 参数 ≈ 82KB、**468 秒**才吐完：这段时间 `content` 是空串、
    没有任何 token 事件、日志也一行不出，前端表现为"卡死了"，而进程其实以 ≈69 token/s
    全速输出。这条心跳就是为了把"它还在写"暴露出来。

    ⚠️ 它只解决「在慢慢吐」；**解决不了「对端静默挂起」**——那种情况没有 chunk 到达，
    自然也没有心跳，得靠 `settings.llm_stream_chunk_timeout_seconds` 兜。

    节流两段：
    ① 先等 `tool_args_progress_after_seconds`：正常的小工具调用几毫秒就跑完了，不该被打扰；
    ② 之后每 `tool_args_progress_interval_seconds` 一条（8 分钟 ≈ 240 条，可回放缓冲扛得住）。

    Args:
        entry: `pending` 里那条工具调用记录，需含 `args_str` / `first_ts` / `last_emit`。
        now: 当前时间（秒）。**显式传入而不是内部取** —— 否则这个判断没法确定性测试。

    Returns:
        可直接下发的事件体（`chars` / `kb` / `elapsed`），或 `None` 表示这次不发。
    """
    if not settings.tool_args_progress_enabled:
        return None

    start = entry.get("first_ts")
    if not start:
        return None

    elapsed = now - float(start)
    if elapsed < settings.tool_args_progress_after_seconds:
        return None

    interval = settings.tool_args_progress_interval_seconds
    if now - float(entry.get("last_emit") or 0.0) < interval:
        return None
    entry["last_emit"] = now

    raw = str(entry.get("args_str") or "")
    # 用 UTF-8 字节数折算 KB：参数里大量是中文，字符数会低估体积（1 汉字 = 3 字节）
    return {
        "id": str(entry.get("id") or ""),
        "name": str(entry.get("name") or ""),
        "chars": len(raw),
        "kb": round(len(raw.encode("utf-8")) / 1024, 1),
        "elapsed": round(elapsed, 1),
    }


async def agent_event_stream(session_id: str, agent_input, user_id: str | None = None):
    """把一次 agent 运行（新消息或审批恢复）转成统一事件序列。

    kind 与 data 约定：
      - "token":        data 为模型输出增量文本（**只含主智能体**）
      - "thinking":     data 为 {"scope","subagent_id","delta"} 推理增量（思考中，实时心跳）
      - "tool_call":    data 为 {"id","name","args"} 工具调用（含参数）
          追加 `intent_label` / `intent_source`：一句给用户看的说明（看板的
        "当前步骤"用它）。`args` **一定是摘掉 `_intent` 的干净参数**。
      - "tool_result":  data 为 {"id","name","content", "ok","exit_code","reason","duration_ms"}
        工具返回结果。 追加的四个字段是**后端判定好的事实**，前端不要自己从
        `content` 里猜：`ok` = true/false/null（null = 判不出来，如空输出）、
        `exit_code` 只在命令有退出码时给、`reason` 是失败原因的人话、
        `duration_ms` = 从发出 `tool_call` 到收到结果的实测耗时。
      - "tool_args":    data 为 {"id","name","chars","kb","elapsed"} —— 工具参数**还在生成**
        时的节流心跳。大参数（实测 82KB）要几十秒到几分钟才吐完，而 `tool_call` 必须等
        参数拼完整才发得出来 ⇒ 没有它这段就是纯黑屏。**只表示"在慢慢吐"**，
        "对端静默挂起"不在它覆盖范围内（见 `args_progress_event` 的说明）。
      - "subagent_start": data 为 {"id","subagent_type","summary"} 子智能体开始（摘要）
      - "subagent_token": data 为 {"id","delta"} 子智能体产出的正文增量
      - "subagent_thinking": data 为 {"id","delta"} 子智能体的推理增量
      - "subagent_tool":  data 为 {"id","name","args","result","status"} 子智能体自己的工具
      - "subagent_end":   data 为 {"id","reason"} 子智能体结束（done/timeout）
      - "subagent_usage": data 为子智能体自己的 token 用量（含 id）
      - "usage":        data 为**主智能体**的 token 用量（精确，来自 usage_metadata）
      - "approval":     data 为待审批信息（session 需要人工审批）
      - "result":       data 为最终回复全文
      - "error":        data 为错误信息

    Args:
        session_id: 会话标识（= langgraph thread_id）。
        agent_input: `{"messages": [...]}` 新消息，或 `Command(resume=...)` 审批恢复。
        user_id: 会话归属者，用于定位该用户的工作区与长期记忆。传空则由
            `session_owner()` 兜底查一次；这里只解析**一次**然后往下传，
            避免每个工具事件都去查一遍库。

    ## 为什么用 subgraphs=True

    deepagents 的 `task` 工具内部是 `await subagent.ainvoke(...)`（不是 astream），
    所以子智能体的过程**不会**出现在父级事件流里 —— 实测父流里只有
    `langgraph_node ∈ {model, tools}`，子智能体那 200+ 条事件一条都不在里面。
    带上 `subgraphs=True` 后，子智能体的事件会以命名空间 `tools:<task调用id>` 出现，
    才可能把它的思考和工具调用实时展示给用户（否则用户只能看着一个转圈，
    无法区分"在干活"和"挂了"）。
    """
    from app.agent.approvals import read_pending_approval

    # 只解析一次归属：下面每个工具事件都要写 user_id，逐个查库没必要
    owner = user_id or session_owner(session_id)
    agent = await get_agent(session_id, user_id=owner)
    config = {"configurable": {"thread_id": session_id}}

    # 新一轮开始前，把上一轮异常中断遗留的 running 记录收尾（否则前端永远转圈）
    mark_stale_tool_calls(session_id)

    # 流式工具调用参数是按 index 分片（tool_call_chunks.args）到达的。
    # 参数一旦拼成完整 JSON，就立刻推送 tool_call（这样 task 委派等长任务能马上显示"执行中"）；
    # 结果仍等 ToolMessage 到达后推送 tool_result。
    pending: list[dict] = []  # {id, name, args_str, emitted}

    # 子图命名空间 id → task 调用 id 的归一表。
    # 不加这一层，卡片（按 tool_call id 建）和它的正文（按命名空间 id 到达）
    # 会对不上，正文被渲染成第二张幽灵卡片（详见 SubagentCallRegistry 注释）。
    subagent_calls = SubagentCallRegistry()

    # call_id → 发出 `tool_call` 的单调时钟。用来算"这一步到底跑了多久"。
    # 放在 `_call_event` 里打点，是**唯一**能覆盖全部发射点的位置（它是三个
    # 发射点的公共入口）；散到各处打点，迟早有路径漏掉，表现是部分卡片没有耗时。
    call_started: dict[str, float] = {}

    def _call_event(call_id: str, tool_name: str, raw_args) -> tuple[dict, tuple[str, dict]]:
        """三个发射点共用：摘 `_intent` → 落库（含投影）→ 造 tool_call 事件。

        集中在这里的理由：`intent_label` 是 `tool_call` 事件契约的一部分，
        **每一个**发射点都必须带上它；散成四份迟早会出现"某条路径漏了"，
        而那种缺失的表现是"看板偶尔空一行"，很难定位。

        Returns:
            `(摘干净的参数, (kind, data))`。
        """
        clean, label, source = _resolve_intent(tool_name, raw_args)
        if call_id:
            call_started[str(call_id)] = time.monotonic()
        _log_tool_event(session_id, "call", call_id, tool_name, args=clean,
                        user_id=owner, intent_label=label, intent_source=source)
        return clean, ("tool_call", {
            "id": call_id, "name": tool_name, "args": clean,
            "intent_label": label, "intent_source": source,
        })

    async for chunk in streaming_astream(agent, agent_input, config):
        namespace, msg, metadata = normalize_chunk(chunk)
        node = metadata.get("langgraph_node")
        subagent_id = parse_subagent_namespace(namespace)

        # ---------- 子智能体内部事件：转发给它自己的委派卡片 ----------
        if subagent_id:
            # 统一改写成 task 调用 id，前端只需一套 id 逻辑
            call_id = subagent_calls.resolve(subagent_id)
            async for kind, data in iter_subagent_events(namespace, msg):
                yield (kind, {**data, "id": call_id})
            usage = log_usage_event(namespace, msg)
            if usage:
                # 子智能体的用量单独成一个事件名（`subagent_usage`）：
                # 它必须落到对应委派卡片上，不能混进主智能体的总速率里。
                yield ("subagent_usage", {**usage, "id": call_id})
            continue

        if node not in ("model", "tools"):
            continue

        # 工具返回（ToolMessage）：补发（若尚未发出）tool_call 后，再发结果
        if isinstance(msg, ToolMessage):
            tc_id = str(getattr(msg, "tool_call_id", "") or "")
            name = str(getattr(msg, "name", "") or "")
            entry = next((p for p in pending if p["id"] == tc_id), None)
            args: dict = {}
            if entry:
                if entry.get("name"):
                    name = entry["name"]
                if entry["args_str"].strip():
                    try:
                        args = json.loads(entry["args_str"])
                    except Exception:
                        args = {}
                if not entry.get("emitted"):
                    args, event = _call_event(tc_id, name, args)
                    yield event
                    if name == "task":
                        yield _subagent_start_event(tc_id, args)
                pending.remove(entry)
            else:
                # 没有 pending 记录（如进程在工具执行中途重启后补发）：参数拿不到，
                # 意图标签无从谈起 → 目录兜底，看板仍然有东西可显示
                _, event = _call_event(tc_id, name, {})
                yield event
            # 委派收尾：告诉前端这张卡片停止流式（超时与否都发，前端据此收尾）
            if name == "task":
                subagent_calls.note_task_finished(tc_id)
                yield ("subagent_end", {"id": tc_id, "reason": "done"})
            # 这一步"成没成、跑了多久"必须由后端判定后随事件下发。
            # 前端自己 grep 结果正文是行不通的：`execute` 的正文里照样可能有
            # "error" 字样，而真正的成败只写在末尾的 `[...]` 尾巴上。
            content = str(getattr(msg, "content", "") or "")
            outcome = classify_tool_result(name, content)
            started = call_started.pop(tc_id, None)
            duration_ms = int((time.monotonic() - started) * 1000) if started else None
            yield ("tool_result", {
                "id": tc_id,
                "name": name,
                "content": content,
                "ok": outcome["ok"],
                "exit_code": outcome["exit_code"],
                "reason": outcome["reason"],
                "duration_ms": duration_ms,
            })
            _log_tool_event(session_id, "result", tc_id, name,
                            result=content, user_id=owner,
                            status="error" if outcome["ok"] is False else "done")
            continue

        # 累积模型节点流式工具调用分片（name/id 在首块，args 逐字到达）
        for tcc in getattr(msg, "tool_call_chunks", None) or []:
            if not isinstance(tcc, dict):
                continue
            idx = tcc.get("index", 0)
            while len(pending) <= idx:
                pending.append({
                    "id": "", "name": "", "args_str": "", "emitted": False,
                    # 参数开始到达的时刻 + 上次发心跳的时刻（见 args_progress_event）
                    "first_ts": time.monotonic(), "last_emit": 0.0,
                })
            e = pending[idx]
            if tcc.get("id"):
                e["id"] = str(tcc["id"])
            if tcc.get("name"):
                e["name"] = str(tcc["name"])
            if tcc.get("args"):
                e["args_str"] += str(tcc["args"])
            # 参数完整即尽早推送 tool_call
            if not e["emitted"] and e["name"] and e["args_str"].strip():
                try:
                    parsed_args = json.loads(e["args_str"])
                except Exception:
                    # 参数还没拼完。大参数在这里会反复走到（实测 82KB / 468 秒），
                    # 而下面这段期间**本来一个事件都不会下发** ⇒ 补一条节流心跳，
                    # 让前端能把"在慢慢写参数"和"卡死了"区分开。
                    progress = args_progress_event(e, time.monotonic())
                    if progress:
                        yield ("tool_args", progress)
                    continue
                e["emitted"] = True
                parsed_args, event = _call_event(e["id"], e["name"], parsed_args)
                yield event
                if e["name"] == "task":
                    subagent_calls.note_task_call(e["id"])
                    yield _subagent_start_event(e["id"], parsed_args)

        # 非流式兜底：模型一次性给出完整 tool_calls（此时 tool_call_chunks 通常为空）
        for tc in getattr(msg, "tool_calls", None) or []:
            if not isinstance(tc, dict) or not (tc.get("id") and tc.get("name")):
                continue
            if not any(p["id"] == str(tc["id"]) for p in pending):
                args = tc.get("args") or {}
                # ⚠️ `args_str` 里存的是**摘过 `_intent` 的干净参数**：它后面会被
                # ToolMessage 分支拿去补发事件/落库（见上面的 `_call_event`），
                # 留着 `_intent` 会在那条路径上二次泄漏。
                args, event = _call_event(str(tc["id"]), str(tc["name"]), args)
                pending.append({
                    "id": str(tc["id"]),
                    "name": str(tc["name"]),
                    "args_str": json.dumps(args, ensure_ascii=False),
                    "emitted": True,
                })
                yield event
                if str(tc["name"]) == "task":
                    subagent_calls.note_task_call(str(tc["id"]))
                    yield _subagent_start_event(str(tc["id"]), args)

        content = getattr(msg, "content", "")
        if isinstance(content, str) and content:
            yield ("token", content)

        # 推理增量：模型"思考"阶段唯一会动的东西。
        # 不转发它，前端在长推理期间就是纯黑屏（实测 31.8s 的回答里前 26.9s 如此），
        # 用户无法区分"在思考"和"挂了" —— 这正是本次要解决的问题。
        think = reasoning_delta(msg)
        if think:
            yield ("thinking", {"scope": "main", "subagent_id": "", "delta": think})

        # 精确 token 用量（每个模型步结束随分片到达）
        usage = log_usage_event(namespace, msg)
        if usage:
            yield ("usage", usage)

    # 检查是否有（新的）审批中断
    try:
        pinfo = await read_pending_approval(agent, config, user_id=owner)
    except Exception as e:
        logger.warning("[runtime] read_pending_approval failed: %s", e)
        pinfo = {"pending": False}
    if pinfo.get("pending"):
        # 停在审批上时，除了让前端弹窗，还必须带上一句"我已经说到哪儿了"：
        # 前端收到 approval 只会弹窗，气泡里什么都没有 —— 用户观感就是
        # "工具都成功跑完了，却一个字都没回我"。text 为空时由前端补一句默认说明。
        try:
            note = await _final_reply_text(agent, config)
        except Exception as e:  # noqa: BLE001
            logger.warning("[runtime] read pending reply text failed: %s", e)
            note = ""
        yield ("approval", {**pinfo, "text": note})
        return

    # 无中断：取最终回复
    try:
        text = await _final_reply_text(agent, config)
    except Exception as e:
        logger.warning("[runtime] read final reply failed: %s", e)
        text = ""
    if not text.strip():
        # 空回复等于"没有回复"（前端只在非空时才写进气泡）→ 用兜底文案说清楚
        text = _empty_reply_fallback(session_id)
    yield ("result", text)


async def stream_chat(session_id: str, user_message: str, user_id: str | None = None):
    """流式对话：一条用户消息 → 事件序列（见 agent_event_stream）。"""
    from app.agent.context import get_checkpointer

    # 在 checkpoint 创建 thread_id 时，确保会话存在于 langgraph checkpoint
    await get_checkpointer()
    async for event in agent_event_stream(
        session_id,
        {"messages": [{"role": "user", "content": user_message}]},
        user_id=user_id,
    ):
        yield event


def resume_input(resume: dict | list):
    """把审批恢复值包成 langgraph 的 `Command(resume=...)`。

    统一在这里构造：调用方（HTTP 路由 / 运行管理器）只需要拿一个恢复值，
    不必各自 import Command、也不必各自记得"扁平列表要包一层 decisions"。

    Args:
        resume: 多中断映射 `{interrupt_id: {"decisions": [...]}}`（推荐），
            或扁平决策列表 `[{"type": "approve"}]`（旧形态，自动包一层）。
    """
    from langgraph.types import Command

    payload = {"decisions": resume} if isinstance(resume, list) else resume
    return Command(resume=payload)


async def stream_resume(session_id: str, resume: dict | list, user_id: str | None = None):
    """审批恢复：让挂起的 graph 继续跑 → 事件序列。

    与 stream_chat 共用同一套事件，前端可以用同一个渲染器消费，
    从而避免"审批后回复与工具调用脱节"的问题。

    注意：HTTP 审批路由**不**走这里 —— 它把恢复交给 `run_manager.start(...)`
    （这样恢复才算该会话的一轮运行：状态诚实、可回放、且不会被新一轮顶掉）。
    本函数保留给不需要 HTTP 语义的调用方（如探针、关闭人工介入时唤醒挂起中断）。

    Args:
        resume: 见 `resume_input`。
    """
    async for event in agent_event_stream(session_id, resume_input(resume), user_id=user_id):
        yield event
