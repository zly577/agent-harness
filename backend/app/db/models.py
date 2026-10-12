"""业务 ORM 模型（MySQL agent_harness）。

模型清单：
Session：会话主记录
ToolCallLog：工具调用流水（兼作 `tool_call` 事件的存储）
TaskStep：任务看板「当前步骤」读模型（派生自 ToolCallLog）
AuditLog：Agent 运行审计
User：登录用户（多租户归属的根）
LlmProvider：模型供应商配置（openai / anthropic 双协议）
McpServer：外部 MCP 工具源配置（按用户；headers/env 加密）

## 归属设计（重要）

`sessions` / `tool_call_logs` / `audit_logs` 都加了 `user_id`，但它**故意是可空的**：

- 存量库里有 87 个会话、900+ 条日志，它们是在 users 表存在之前创建的；
- 迁移如果直接加 `NOT NULL` 列，在老库上根本执行不过去；
- 即使再加一个"回填"步骤，回填之后**任何**旧代码路径（或回滚一半的进程）
  再写入无主行，`NOT NULL` 就会让整轮对话 500 —— 这比"多一个 NULL"危险得多。

所以策略是：**DDL 允许 NULL，归属由应用层强制，启动时兜底回填**
（`app/db/bootstrap.py`）。未归属的行在查询里是 `user_id IS NULL`，
`WHERE user_id = <当前用户>` 永远匹配不到它 —— 即 **fail-closed**：
最坏情况是"谁都看不到"，绝不会"别人看得到"。
"""
from datetime import datetime

from sqlalchemy import Boolean, DateTime, Integer, String, Text, UniqueConstraint, func
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base


class User(Base):
    """登录用户。多租户隔离的根：所有业务资源都往上挂 `user_id`。"""

    __tablename__ = "users"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, comment="用户 ID（uuid4 hex）")
    email: Mapped[str] = mapped_column(
        String(255), unique=True, index=True, comment="登录邮箱（唯一）"
    )
    password_hash: Mapped[str] = mapped_column(
        String(255), default="", comment="argon2id 哈希（绝不存明文）"
    )
    display_name: Mapped[str] = mapped_column(String(128), default="", comment="展示名")
    role: Mapped[str] = mapped_column(String(16), default="user", comment="user / admin")
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, comment="禁用后无法登录")
    created_at: Mapped[datetime] = mapped_column(
        DateTime, server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, server_default=func.now(), onupdate=func.now(), nullable=False
    )


class Session(Base):
    """会话主记录。ID 与 langgraph SQLite checkpoint 的 thread_id 对齐。"""

    __tablename__ = "sessions"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, comment="会话 ID（=checkpoint thread_id）")
    #: 所属用户。可空仅为了兼容存量数据，新会话一律带值（见模块头注释）
    user_id: Mapped[str | None] = mapped_column(
        String(32), index=True, nullable=True, comment="所属用户；NULL=存量待回填"
    )
    #: 本会话选定的供应商。为空则按"用户默认 → 系统默认"解析
    provider_id: Mapped[str | None] = mapped_column(
        String(32), index=True, nullable=True, comment="会话绑定的模型供应商；NULL=跟随默认"
    )
    title: Mapped[str] = mapped_column(String(255), default="新会话", comment="会话标题")
    status: Mapped[str] = mapped_column(String(16), default="active", comment="active/archived")
    created_at: Mapped[datetime] = mapped_column(
        DateTime, server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, server_default=func.now(), onupdate=func.now(), nullable=False
    )
    note: Mapped[str] = mapped_column(Text, default="", comment="备注")


class ToolCallLog(Base):
    """工具调用流水（持久化）。

    流式对话中每次 tool_call / tool_result 事件即时落库；用于服务重启后
    回放历史会话的工具调用面板——checkpoint 里的消息可能被上下文压缩
    （SummarizationMiddleware）裁剪，或在运行中途被关闭而不完整。

    ## 这张表 = `tool_call` 事件的**事件存储**

    `intent_label` / `intent_source` 就是数据契约里那两个字段。它们的取值
    规律（`resolve_intent` 保证）：

    - `intent_label` 是规范化后的展示文案（≤24 字符、单行）；
    - `intent_source` ∈ {model, fallback, backfill}；
    - 两列**要么都有值、要么都是 NULL**（NULL = 该功能关闭，或这行是
      功能上线前的历史数据、回填脚本还没跑到）。有值就一定有来源，
      否则前端无法判断"这句是人话还是机器兜底"。

    注意 `args` 里**不含** `_intent`（下发给前端与外部服务的是干净参数）；
    模型原始输出的消息体仍原样躺在 checkpoint 里，要追溯看那里。
    """

    __tablename__ = "tool_call_logs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    session_id: Mapped[str] = mapped_column(String(32), index=True, comment="会话 ID（=thread_id）")
    user_id: Mapped[str | None] = mapped_column(
        String(32), index=True, nullable=True, comment="所属用户（冗余自会话，便于直接按用户查）"
    )
    call_id: Mapped[str] = mapped_column(String(64), default="", comment="工具调用 ID")
    name: Mapped[str] = mapped_column(String(128), default="", comment="工具名")
    kind: Mapped[str] = mapped_column(String(16), default="tool", comment="tool=普通工具；task=子智能体委派")
    args: Mapped[str] = mapped_column(Text, default="", comment="调用参数（JSON 字符串，不含 _intent）")
    result: Mapped[str] = mapped_column(Text, default="", comment="工具返回结果")
    status: Mapped[str] = mapped_column(String(16), default="running", comment="running/done/error/interrupted")
    #: 规范化后的展示文案（≤24 字符、单行）；NULL = 无标签（功能关闭/待回填）
    intent_label: Mapped[str | None] = mapped_column(
        String(64), nullable=True, comment=" 意图标签（展示文案，≤24 字符）"
    )
    #: 标签来源（model / fallback / backfill）；与 intent_label 同时有值
    intent_source: Mapped[str | None] = mapped_column(
        String(16), nullable=True, comment=" 标签来源：model/fallback/backfill"
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime, server_default=func.now(), nullable=False
    )


class TaskStep(Base):
    """任务看板的「当前步骤」读模型。

    ## 它是派生数据，权威来源是 `tool_call_logs`

    每一行 = 一次工具调用在看板上的样子。构建器
    （`app/db/projections.py::project_session`）**只**从 `tool_call_logs` 读、
    按 `(session_id, call_id)` 幂等写入，所以：

    - 可以随时整会话重建，结果逐字段相同（回放确定性）；
    - `tool_call_logs` 被清理时，这里的孤儿行也会被同一次构建删掉。

    ## 为什么不让看板直接查 `tool_call_logs`

    因为它们回答的问题不同：日志表要保留**全部**发生过的调用（含被中断、
    含已被上下文压缩裁剪的），而看板只关心"用户看到的那几行字"。将来要给
    看板加字段（图标、预估耗时、重试入口）时，改这里不会污染事件存储。

    `user_id` 同 `tool_call_logs`：可空，仅为兼容"会话在 users 表之前就存在"
    这类存量数据；查询永远按 `session_id` 定位（session_id 是不可猜的 uuid hex，
    且路由层已做归属校验，见 `api/routes/sessions.py`）。
    """

    __tablename__ = "task_steps"
    __table_args__ = (
        UniqueConstraint("session_id", "call_id", name="uq_task_steps_session_call"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    session_id: Mapped[str] = mapped_column(String(32), index=True, comment="会话 ID（=thread_id）")
    user_id: Mapped[str | None] = mapped_column(
        String(32), index=True, nullable=True, comment="所属用户；NULL=存量待回填"
    )
    call_id: Mapped[str] = mapped_column(String(64), default="", comment="工具调用 ID（幂等键）")
    step_index: Mapped[int] = mapped_column(Integer, default=0, comment="会话内顺序（从 1 开始）")
    tool_name: Mapped[str] = mapped_column(String(128), default="", comment="工具名")
    display_name: Mapped[str] = mapped_column(
        String(128), default="", comment="目录里的展示名（未知工具 = 工具名本身）"
    )
    kind: Mapped[str] = mapped_column(String(16), default="tool", comment="tool / task")
    status: Mapped[str] = mapped_column(
        String(16), default="running", comment="running/done/error/interrupted"
    )
    intent_label: Mapped[str | None] = mapped_column(
        String(64), nullable=True, comment=" 意图标签（与 tool_call 事件同值）"
    )
    intent_source: Mapped[str | None] = mapped_column(
        String(16), nullable=True, comment=" 标签来源：model/fallback/backfill"
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime, server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, server_default=func.now(), onupdate=func.now(), nullable=False
    )


class AuditLog(Base):
    """Agent 运行审计记录。"""

    __tablename__ = "audit_logs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    thread_id: Mapped[str] = mapped_column(String(64), index=True, default="", comment="会话/thread ID")
    user_id: Mapped[str | None] = mapped_column(
        String(32), index=True, nullable=True, comment="所属用户（按 thread_id 归属回填）"
    )
    run_id: Mapped[str] = mapped_column(String(64), default="", comment="run ID")
    action: Mapped[str] = mapped_column(String(32), comment="audit 事件类型，如 agent_run / tool_call / rate_limited")
    detail: Mapped[str] = mapped_column(Text, default="", comment="事件详情（JSON 字符串）")
    created_at: Mapped[datetime] = mapped_column(
        DateTime, server_default=func.now(), nullable=False, index=True
    )


class LlmProvider(Base):
    """模型供应商配置。一个供应商 + 一个模型 = 一条记录。

    ## `user_id` 为空 = 系统级共享供应商

    - `user_id IS NULL`：所有用户都能用；只有 admin 能改（一般是迁移时由 `.env`
      的 DeepSeek 配置引导写入的那条，或管理员手工加的内网模型服务）。
    - `user_id = <某人>`：该用户的私有配置，别人既看不见也用不了。

    ## api_key 的两种形态

    - `api_key_encrypted`：Fernet 密文（真正拿去请求的那个）；
    - `api_key_hint`：落库时就算好的掩码（如 `sk-****1234`）。
      列表接口只回掩码，**因此"看一眼供应商列表"不会解密任何密钥**。
    """

    __tablename__ = "llm_providers"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, comment="供应商 ID（uuid4 hex）")
    user_id: Mapped[str | None] = mapped_column(
        String(32), index=True, nullable=True, comment="NULL=系统级共享；非空=用户私有"
    )
    name: Mapped[str] = mapped_column(String(128), default="", comment="展示名，如「我的 DeepSeek」")
    protocol: Mapped[str] = mapped_column(
        String(16), default="openai", comment="openai / anthropic"
    )
    base_url: Mapped[str | None] = mapped_column(
        String(512), nullable=True, comment="留空用官方默认地址"
    )
    api_key_encrypted: Mapped[str] = mapped_column(Text, default="", comment="Fernet 密文")
    api_key_hint: Mapped[str] = mapped_column(String(32), default="", comment="掩码，列表展示用")
    model: Mapped[str] = mapped_column(String(128), default="", comment="模型名，如 deepseek-chat")
    parameters: Mapped[str] = mapped_column(
        Text, default="", comment="JSON：temperature / timeout / max_tokens 等，空=系统默认"
    )
    is_default: Mapped[bool] = mapped_column(
        Boolean, default=False, comment="每 owner 至多一个为真（应用层事务保证）"
    )
    enabled: Mapped[bool] = mapped_column(Boolean, default=True, comment="停用后不参与解析")
    created_at: Mapped[datetime] = mapped_column(
        DateTime, server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, server_default=func.now(), onupdate=func.now(), nullable=False
    )


class McpServer(Base):
    """外部 MCP 工具源配置。

    ## 为什么是**按用户**而不是全局

    工作区、长期记忆、模型供应商都是按用户隔离的，MCP 若还是"全局
    YAML + 全局生效"，就会出现"A 配了一个能读内网的 MCP、B 的 agent 里也挂着它"。
    所以数据库里的每一行都归某个用户；`user_id` **不可空**（这张表没有存量数据，
    不存在"加 NOT NULL 跑不过去"的历史包袱）。

    `backend/configs/mcp/*.yaml` 那份来源保留，但它对所有人生效，语义等于
    "运维预置"，日常新增应走数据库。

    ## 密钥

    `headers` / `env` 里常有 `Authorization`、`API_KEY`，整段加密后存
    （Fernet，与供应商 api_key 同一套）。列表接口想展示"配了哪些头"时不能解密，
    所以额外存一份 `header_keys`（**只有键名**）专门用于展示。
    """

    __tablename__ = "mcp_servers"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, comment="MCP server ID（uuid4 hex）")
    user_id: Mapped[str] = mapped_column(
        String(32), index=True, nullable=False, comment="所属用户（不可空：不存在全局共享的 MCP）"
    )
    name: Mapped[str] = mapped_column(String(128), default="", comment="唯一名（同一用户下不重复）")
    transport: Mapped[str] = mapped_column(
        String(16), default="streamable_http",
        comment="streamable_http / sse / stdio / websocket",
    )
    url: Mapped[str] = mapped_column(String(1024), default="", comment="远程端点（stdio 为空）")
    headers_encrypted: Mapped[str] = mapped_column(
        Text, default="", comment="Fernet 密文（JSON dict：HTTP 头，可能含密钥）"
    )
    header_keys: Mapped[str] = mapped_column(
        Text, default="", comment="JSON list[str]：只存头名，列表展示用（避免为展示而解密）"
    )
    command: Mapped[str] = mapped_column(String(512), default="", comment="stdio：启动命令")
    args_json: Mapped[str] = mapped_column(Text, default="", comment="stdio：命令参数（JSON list[str]）")
    env_encrypted: Mapped[str] = mapped_column(
        Text, default="", comment="Fernet 密文（JSON dict：子进程环境变量，可能含密钥）"
    )
    cwd: Mapped[str] = mapped_column(String(512), default="", comment="stdio：子进程工作目录")
    enabled: Mapped[bool] = mapped_column(Boolean, default=True, comment="停用后不加载")
    created_at: Mapped[datetime] = mapped_column(
        DateTime, server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, server_default=func.now(), onupdate=func.now(), nullable=False
    )


class RegistrationCode(Base):
    """One active registration challenge per normalized email; consumed atomically."""
    __tablename__ = "registration_codes"
    email: Mapped[str] = mapped_column(String(255), primary_key=True)
    code_hash: Mapped[str] = mapped_column(String(255))
    sent_at: Mapped[datetime] = mapped_column(DateTime)
    expires_at: Mapped[datetime] = mapped_column(DateTime)
    attempts: Mapped[int] = mapped_column(Integer, default=0)
