# Agent Harness 全阶段代码汇编

> 本文档汇总了本项目（D:\Deepseek_Harness\agent-harness）从 P0 到 P10 每一步添加/修改的核心代码，
> 以便整体审查与复盘。不同阶段的完整文件以实际源码为准，此处摘录关键实现。
>
> 阶段路线：P0 骨架 → P1 最小 Agent → P2 上下文持久化 → P3 虚拟FS沙箱 → P4 Skills →
> P5 中间件 → P6 多智能体 → P7 MCP → P8 人工介入+安全 → P9 Vue3 前端 → P10 部署。

---

## 一、环境与技术栈

- Agent 框架：deepagents 0.7.1（LangChain 1.x + LangGraph 1.2.x），运行于 conda `agent_env`（Python 3.12）。
- 后端：FastAPI + Pydantic v2；模型：DeepSeek `deepseek-v4-flash`（OpenAI 兼容）。
- 存储：SQLite（langgraph checkpoint/store，消息历史）+ MySQL 8.0（业务数据）。
- 前端：Vue3 + Vite（SSE 流式）。

### 运行环境（P0 决策）：app/core/config.py

```python
"""全局配置：从 .env / 环境变量读取（pydantic-settings）。"""
from functools import lru_cache
from pathlib import Path
from pydantic_settings import BaseSettings, SettingsConfigDict

_BACKEND_DIR = Path(__file__).resolve().parent.parent.parent


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    app_name: str = "Agent Harness"
    app_version: str = "0.1.0"
    app_env: str = "development"

    # DeepSeek / LLM
    deepseek_api_key: str = ""
    deepseek_base_url: str = "https://api.deepseek.com"
    deepseek_model: str = "deepseek-v4-flash"

    # 持久化
    mysql_url: str = "mysql+pymysql://root:CHANGE_ME@127.0.0.1:3306/agent_harness?charset=utf8mb4"
    sqlite_path: str = str(_BACKEND_DIR / "data" / "harness.sqlite")

    # 上下文管理
    context_max_messages: int = 80
    context_keep_messages: int = 24

    # MCP 开关
    mcp_enabled: bool = False


@lru_cache
def get_settings() -> Settings:
    return Settings()


settings = get_settings()
```

---

## 二、P0 项目骨架

### 入口：app/main.py

```python
from contextlib import asynccontextmanager
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from app.api.routes import api_router
from app.core.config import settings


@asynccontextmanager
async def lifespan(app: FastAPI):
    # 启动时自动建业务表（MySQL）
    from app.db.base import Base
    from app.db.session import engine
    import app.db.models  # noqa
    Base.metadata.create_all(bind=engine)
    logger.info("[P2] MySQL 业务表 ready")
    yield


app = FastAPI(title=settings.app_name, version=settings.app_version, lifespan=lifespan)
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_credentials=False,
                   allow_methods=["*"], allow_headers=["*"])
app.include_router(api_router)


@app.get("/")
def root():
    return {"app": settings.app_name, "version": settings.app_version, "docs": "/docs"}


@app.get("/health")
def health():
    return {"status": "ok", "app": settings.app_name, "version": settings.app_version, "env": settings.app_env}
```

### 数据库基类与会话：app/db/base.py + session.py

```python
# base.py
from sqlalchemy.orm import DeclarativeBase
class Base(DeclarativeBase):
    """所有业务 ORM 模型的基类。"""

# session.py
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from app.core.config import settings

engine = create_engine(settings.mysql_url, pool_pre_ping=True, pool_recycle=3600)
SessionLocal = sessionmaker(bind=engine, autocommit=False, autoflush=False)


def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()
```

### .env.example 模板

```ini
DEEPSEEK_API_KEY=sk-your-key-here
DEEPSEEK_BASE_URL=https://api.deepseek.com
DEEPSEEK_MODEL=deepseek-v4-flash
MYSQL_URL=mysql+pymysql://root:CHANGE_ME@127.0.0.1:3306/agent_harness?charset=utf8mb4
CONTEXT_MAX_MESSAGES=80
CONTEXT_KEEP_MESSAGES=24
MCP_ENABLED=false
APP_ENV=development
```

---

## 三、P1 最小 Agent

### 模型工厂：app/agent/model.py

```python
from langchain_deepseek import ChatDeepSeek
from app.core.config import settings


def build_llm() -> ChatDeepSeek:
    return ChatDeepSeek(
        model=settings.deepseek_model,
        api_key=settings.deepseek_api_key,
        base_url=settings.deepseek_base_url,
        temperature=0,
        timeout=60,
    )
```

### 对话接口：app/schemas/chat.py + app/api/routes/chat.py

```python
# schemas/chat.py
from pydantic import BaseModel, Field

class ChatRequest(BaseModel):
    message: str = Field(..., min_length=1, max_length=8000)
    session_id: str | None = Field(None)

class ChatResponse(BaseModel):
    session_id: str
    reply: str

# api/routes/chat.py
from fastapi import APIRouter, HTTPException
from app.agent import runtime
from app.schemas.chat import ChatRequest, ChatResponse

router = APIRouter(prefix="/api/v1", tags=["chat"])

@router.post("/chat", response_model=ChatResponse)
async def chat(req: ChatRequest):
    if req.session_id is None:
        session_id = runtime.create_session()
    elif runtime.session_exists(req.session_id):
        session_id = req.session_id
    else:
        raise HTTPException(status_code=404, detail="session not found")
    try:
        reply = await runtime.run_chat(session_id, req.message)
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"agent error: {type(e).__name__}: {e}")
    return ChatResponse(session_id=session_id, reply=reply)
```

### 初版运行时（P1：内存二元组——P2 改为真实 BaseMessage）

```python
# app/agent/runtime.py（P1 关键）
_sessions: dict[str, list[tuple[str, str]]] = {}

def new_session() -> str:
    session_id = uuid.uuid4().hex
    _sessions[session_id] = []
    return session_id

async def run_chat(session_id, user_message):
    history = _sessions[session_id]
    messages = history + [("user", user_message)]
    result = await get_agent().ainvoke({"messages": messages})
    reply = result["messages"][-1].content
    history.append(("user", user_message)); history.append(("assistant", reply))
    return reply
```

---

## 四、P2 上下文管理（SQLite checkpoint/store + MySQL 业务库）

### 上下文：app/agent/context.py

```python
from pathlib import Path
import aiosqlite
from langchain.agents.middleware import SummarizationMiddleware
from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver
from langgraph.store.sqlite.aio import AsyncSqliteStore
from app.agent.model import build_llm
from app.core.config import settings

_saver, _store, _conn = None, None, None

async def _ensure_conn():
    global _conn
    if _conn is None:
        path = Path(settings.sqlite_path)
        path.parent.mkdir(parents=True, exist_ok=True)
        _conn = await aiosqlite.connect(str(path))
    return _conn

async def get_checkpointer():
    global _saver
    if _saver is None:
        _saver = AsyncSqliteSaver(await _ensure_conn())
        await _saver.setup()
    return _saver

async def get_store():
    global _store
    if _store is None:
        _store = AsyncSqliteStore(await _ensure_conn())
        await _store.setup()
    return _store

def build_summary_middleware():
    """上下文压缩：>12 条消息时模型摘要，保留最近 6 条。"""
    return SummarizationMiddleware(
        build_llm(),
        trigger=("messages", settings.context_max_messages),
        keep=("messages", settings.context_keep_messages),
    )
```

### 业务模型：app/db/models.py（Session + AuditLog）

```python
from sqlalchemy import DateTime, Integer, String, Text, func
from sqlalchemy.orm import Mapped, mapped_column
from app.db.base import Base

class Session(Base):
    __tablename__ = "sessions"
    id: Mapped[str] = mapped_column(String(32), primary_key=True)
    title: Mapped[str] = mapped_column(String(255), default="新会话")
    status: Mapped[str] = mapped_column(String(16), default="active")
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now(), onupdate=func.now())
    note: Mapped[str] = mapped_column(Text, default="")

class AuditLog(Base):
    __tablename__ = "audit_logs"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    thread_id: Mapped[str] = mapped_column(String(64), index=True, default="")
    run_id: Mapped[str] = mapped_column(String(64), default="")
    action: Mapped[str] = mapped_column(String(32))
    detail: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now(), index=True)
```

### P2 运行时（真实 BaseMessage + thread_id）

```python
# app/agent/runtime.py（P2 关键）
async def get_agent():
    global _agent
    if _agent is None:
        _agent = create_deep_agent(
            model=build_llm(),
            system_prompt="你是一个通用智能体助手……",
            name="assistant",
            checkpointer=await get_checkpointer(),
            store=await get_store(),
            middleware=[build_summary_middleware()],
        )
    return _agent

def create_session():
    session_id = uuid.uuid4().hex
    db = SessionLocal()
    try:
        db.add(DbSession(id=session_id, title="新会话", status="active"))
        db.commit()
    finally:
        db.close()
    return session_id

async def run_chat(session_id, user_message):
    agent = await get_agent()
    result = await agent.ainvoke(
        {"messages": [{"role": "user", "content": user_message}]},
        config={"configurable": {"thread_id": session_id}},
    )
    reply = result["messages"][-1].content
    return reply if isinstance(reply, str) else str(reply)
```

---

## 五、P3 虚拟文件系统与执行路由（P11 已演进，见第十三章）

> **状态说明**：P3 时期的 `build_main_backend()` 返回 `StoreBackend`（纯内存虚拟 FS，
> 不可执行）。P11 起它返回「本地磁盘读写 + OpenSandbox 执行」的 `CompositeBackend`；
> 原 `StoreBackend` 形态保留为 `build_store_backend()` 备用。

### 沙箱后端：app/agent/sandbox.py（P11 现状）

```python
def build_main_backend(session_key="default", workspace=None):
    """本地磁盘读写（FilesystemBackend）+ OpenSandbox 执行（execute 工具）。"""
    backend = build_hybrid_backend(
        session_key=session_key, root_dir=workspace,
        sync_before_execute=settings.sandbox_sync_enabled,
        sync_after_execute=settings.sandbox_sync_enabled,
    )
    # 占位 route：让 HITL 审批的权限路径通过 deepagents 的 route 校验（读写位置不变）
    backend.routes[PERMISSION_ROUTE_PREFIX] = backend.default
    backend.sorted_routes = sorted(backend.routes.items(), key=lambda x: len(x[0]), reverse=True)
    return backend


def build_store_backend():
    """备用：StoreBackend + InMemoryStore（内存虚拟 FS，可写、不可执行）。"""
    from deepagents.backends import StoreBackend
    from langgraph.store.memory import InMemoryStore
    return StoreBackend(namespace=_namespace_factory(), store=InMemoryStore())


def execute_allowed() -> bool:
    """执行总开关：跟随 SANDBOX_ENABLED。"""
    return bool(settings.sandbox_enabled)


def default_permissions():
    """P8 审批用：写 /workspace/** 触发人工审批（mode='interrupt'）。"""
    from deepagents.middleware.filesystem import FilesystemPermission
    return [
        FilesystemPermission(operations=["write"],
                             paths=[WORKSPACE_PREFIX + "/**"],
                             mode="interrupt"),
    ]
```

### P3 关键技术结论（StoreBackend async+sync 适配，仍然有效）

- StoreBackend 内部 `awrite/aread` 用 async store API，`ls/glob` 用同步 store.search；
  需给一个**同时支持 async+sync** 的 store → 采用 `InMemoryStore`（实测全兼容）。
- 权限路径须用 **glob 通配**（`/workspace/**`），目录结尾（`/workspace/`）不匹配子孙路径。

---

## 六、P4 Skills（YAML 定义 → SKILL.md → SkillsMiddleware）

### 技能注册表：app/agent/skills.py

```python
from pathlib import Path
import yaml
from pydantic import BaseModel, Field, field_validator

_COMPILED_DIR = Path(__file__).resolve().parent.parent.parent / "configs" / "skills"
DEFINITIONS_DIR = _COMPILED_DIR / "definitions"
BACKEND_SKILLS_ROOT = "/skills/"


class SkillDefinition(BaseModel):
    name: str
    description: str
    allowed_tools: list[str] = Field(default_factory=list)
    instructions: str

    @field_validator("name")
    @classmethod
    def _ok(cls, v):
        if not v or not all(c.islower() or c.isdigit() or c == "-" for c in v):
            raise ValueError("name 必须小写字母/数字/连字符")
        return v


def _render_skill_md(defn):
    fm = {"name": defn.name, "description": defn.description,
          "allowed-tools": defn.allowed_tools}
    head = "---\n" + "\n".join(f"{k}: {v}" for k, v in fm.items()) + "\n---\n"
    return head + f"# {defn.name}\n\n{defn.instructions.strip()}\n"


def load_all_definitions() -> list[SkillDefinition]:
    result = []
    for f in sorted(DEFINITIONS_DIR.glob("*.yaml")):
        data = yaml.safe_load(f.read_text(encoding="utf-8")) or {}
        result.append(SkillDefinition(**data))
    return result


async def seed_skills(backend):
    """把编译出的 SKILL.md 播种进 backend 虚拟文件系统，供 SkillsMiddleware 扫描。"""
    for skill_dir in _COMPILED_DIR.iterdir():
        if not skill_dir.is_dir() or not (skill_dir / "SKILL.md").exists():
            continue
        for f in skill_dir.rglob("*"):
            if f.is_file():
                virt = f"{BACKEND_SKILLS_ROOT}{skill_dir.name}/{f.relative_to(skill_dir).as_posix()}"
                await backend.awrite(virt, f.read_text(encoding="utf-8", errors="replace"))


def build_skills_middleware(backend):
    from deepagents.middleware.skills import SkillsMiddleware
    return SkillsMiddleware(backend=backend, sources=[BACKEND_SKILLS_ROOT])
```

### 示例技能：configs/skills/definitions/greet-skill.yaml

```yaml
name: greet-skill
description: 用中文亲切地打招呼，并自我介绍。
allowed_tools: []
instructions: |
  ## 用途
  当用户请求打招呼或自我介绍时，使用本技能。
  ## 步骤
  1. 用中文热情地问候用户。2. 简要说明可提供的帮助。3. 询问需求。
```

### 技能路由：app/api/routes/skills.py

```python
@router.get("/", response_model=list[str])
def list_skills():
    return skills.available_skills()

@router.post("/", response_model=CreateSkillResponse, status_code=201)
def create_skill(req: CreateSkillRequest):
    """新增技能定义（写为草稿态 YAML，审批后生效）。"""
    path = skills.create_skill_definition(name=req.name, description=req.description,
                                          instructions=req.instructions, allowed_tools=req.allowed_tools)
    return CreateSkillResponse(name=req.name, status="draft", skill_file=str(path))
```

---

## 七、P5 中间件（日志/审计/限流）

### 中间件：app/agent/middleware.py

```python
from langchain.agents.middleware.types import AgentMiddleware
import json, logging, time
logger = logging.getLogger(__name__)


class AuditMiddleware(AgentMiddleware):
    """aafter_agent 写 MySQL audit_logs。"""
    def __init__(self):
        self._model_calls, self._tool_calls, self._tool_names = 0, 0, []
        self._start_ts = 0.0

    @property
    def name(self):
        return "harness-audit"

    async def abefore_agent(self, state, runtime):
        self._model_calls = self._tool_calls = 0
        self._tool_names = []
        self._start_ts = time.time()

    async def aafter_model(self, state, runtime):
        self._model_calls += 1

    async def awrap_tool_call(self, request, handler):
        self._tool_calls += 1
        self._tool_names.append(request.tool_call.get("name", ""))
        return await handler(request)

    async def aafter_agent(self, state, runtime):
        detail = {"messages": len(_safe_messages(state)), "model_calls": self._model_calls,
                  "tool_calls": self._tool_calls, "tools": self._tool_names,
                  "elapsed_ms": int((time.time() - self._start_ts) * 1000)}
        try:
            await _write_audit(runtime.execution_info.thread_id or "",
                               runtime.execution_info.run_id or "", "agent_run", detail)
        except Exception as e:
            logger.warning("[harness] audit write failed: %s", e)


async def _write_audit(thread_id, run_id, action, detail):
    from app.db.models import AuditLog
    from app.db.session import SessionLocal
    db = SessionLocal()
    try:
        db.add(AuditLog(thread_id=thread_id, run_id=run_id, action=action,
                        detail=json.dumps(detail, ensure_ascii=False)))
        db.commit()
    finally:
        db.close()


class LoggingMiddleware(AgentMiddleware):
    """结构化运行日志。"""
    @property
    def name(self):
        return "harness-logging"

    async def awrap_tool_call(self, request, handler):
        logger.info("[harness] tool: %s", request.tool_call.get("name"))
        return await handler(request)

    async def aafter_agent(self, state, runtime):
        logger.info("[harness] run done: thread=%s", runtime.execution_info.thread_id)


class RateLimitMiddleware(AgentMiddleware):
    """可选消息上限护栏。"""
    def __init__(self, max_messages: int = 0):
        self._max = max_messages
        self._count = 0

    @property
    def name(self):
        return "harness-rate-limit"

    async def abefore_agent(self, state, runtime):
        self._count = len(_safe_messages(state))

    async def abefore_model(self, state, runtime):
        if self._max > 0 and self._count > self._max:
            raise RuntimeError(f"[harness] rate limit exceeded: {self._count} > {self._max}")


def _safe_messages(state):
    try:
        return state.get("messages") or []
    except Exception:
        return []
```

---

## 八、P6 多智能体（子智能体 YAML 注册表 + Task 委派 + 隔离）

### 子智能体注册表：app/agent/subagents.py

```python
from pathlib import Path
import yaml
from pydantic import BaseModel, Field, field_validator

_CONFIG_DIR = Path(__file__).resolve().parent.parent.parent / "configs" / "subagents"


class SubAgentConfig(BaseModel):
    name: str
    description: str
    system_prompt: str
    tools: list[str] = Field(default_factory=list)          # 空=继承主 agent 默认工具
    permissions_allow_read: list[str] = Field(default_factory=list)
    permissions_allow_write: list[str] = Field(default_factory=list)

    @field_validator("name")
    @classmethod
    def _name_ok(cls, v):
        if not v or any(not (c.isalnum() or c in "_-") for c in v):
            raise ValueError("name 只能含字母/数字/下划线/连字符")
        return v


def load_all_subagents_cfg() -> list[SubAgentConfig]:
    result, seen = [], set()
    for f in sorted(_CONFIG_DIR.glob("*.yaml")):
        cfg = SubAgentConfig(**(yaml.safe_load(f.read_text(encoding="utf-8")) or {}))
        if cfg.name in seen:
            raise RuntimeError(f"子智能体 name 重复: {cfg.name}")
        seen.add(cfg.name)
        result.append(cfg)
    return result


def build_subagent(cfg: SubAgentConfig):
    """构建 deepagents SubAgent。**不传 model → 继承主 Agent 模型**（同一 DeepSeek）。"""
    from deepagents import SubAgent
    from deepagents.middleware.filesystem import FilesystemPermission
    permissions = []
    if cfg.permissions_allow_read:
        permissions.append(FilesystemPermission(operations=["read"],
                                                paths=cfg.permissions_allow_read, mode="allow"))
    if cfg.permissions_allow_write:
        permissions.append(FilesystemPermission(operations=["write"],
                                                paths=cfg.permissions_allow_write, mode="allow"))
    return SubAgent(name=cfg.name, description=cfg.description,
                    system_prompt=cfg.system_prompt, permissions=permissions or None)


def load_all_subagents() -> list:
    """供 create_deep_agent(subagents=...) 使用。"""
    return [build_subagent(c) for c in load_all_subagents_cfg()]
```

### 示例子智能体：configs/subagents/researcher.yaml

```yaml
name: researcher
description: 负责调研复杂问题、整理信息的专业研究型子代理。
system_prompt: |
  你是一个专业的研究助手，运行在隔离的子代理上下文中。
  你的最终回复会被主代理看到，请给出完整、自包含、结构化的结论。
permissions_allow_read:
  - /
permissions_allow_write:
  - /notes/
```

### runtime 接入（P6 关键）

```python
# app/agent/runtime.py get_agent() 中
_agent = create_deep_agent(
    model=build_llm(),
    system_prompt="你是一个通用智能体助手……",
    name="assistant",
    checkpointer=await get_checkpointer(),
    store=await get_store(),
    backend=backend,
    middleware=middlewares + [skills_mw],
    tools=extra_tools or None,
    permissions=default_permissions(),          # P8 审批
    subagents=load_all_subagents(),             # P6：子智能体（task 工具委派）
)
```

### 隔离机制说明

- deepagents `task` 工具官方说明："Launch ... in an **isolated context window**"——子代理独立上下文。
- 权限隔离：`permissions_allow_*` 映射为 FilesystemPermission；execute 延续 P3 默认禁用。
- 同一模型：SubAgent 省略 model 即继承主 Agent 的 DeepSeek。

---

## 九、P7 MCP 可插拔接入

### 适配层：app/agent/mcp.py

```python
from pathlib import Path
import yaml
from pydantic import BaseModel, Field

_CONFIG_DIR = Path(__file__).resolve().parent.parent.parent / "configs" / "mcp"
_SUPPORTED_TYPES = {"streamable_http", "stdio"}


class McpServerConfig(BaseModel):
    name: str
    type: str = "streamable_http"
    url: str = ""
    headers: dict[str, str] = Field(default_factory=dict)
    command: str = Field(default="", description="stdio 启动命令")
    args: list[str] = Field(default_factory=list)
    env: dict[str, str] = Field(default_factory=dict)
    enabled: bool = True

    def model_post_init(self, __context) -> None:
        if self.type not in _SUPPORTED_TYPES:
            raise ValueError(f"type 必须为 {sorted(_SUPPORTED_TYPES)}")
        if self.type == "streamable_http" and not self.url:
            raise ValueError("streamable_http 必须有 url")
        if self.type == "stdio" and not self.command:
            raise ValueError("stdio 必须有 command")


def load_all_mcp_cfg() -> list[McpServerConfig]:
    result, seen = [], set()
    for f in sorted(_CONFIG_DIR.glob("*.yaml")):
        if f.name.endswith(".example.yaml") or f.name.startswith("."):
            continue
        data = yaml.safe_load(f.read_text(encoding="utf-8")) or {}
        if "mcpServers" in data and isinstance(data["mcpServers"], dict):
            for name, srv in data["mcpServers"].items():
                row = dict(srv) if isinstance(srv, dict) else {}
                row["name"] = srv.get("name", name) if isinstance(srv, dict) else name
                result.append(McpServerConfig(**row))
        else:
            result.append(McpServerConfig(**data))
    return result


def _has_adapters():
    import importlib.util
    return importlib.util.find_spec("langchain_mcp_adapters") is not None


async def build_mcp_tools() -> tuple[list, list[str]]:
    """返回 (tools, errors)。未装适配器时安全降级 ([], [提示])，不崩溃。"""
    tools, errors = [], []
    if not _has_adapters():
        errors.append("langchain_mcp_adapters 未安装。网络恢复并 pip install mcp langchain-mcp-adapters 后启用。")
        return tools, errors
    for cfg in load_all_mcp_cfg():
        if not cfg.enabled:
            continue
        try:
            from langchain_mcp_adapters.tools import load_mcp_tools
            from mcp.client.streamable_http import StreamableHttpTransport
            async with StreamableHttpTransport(url=cfg.url, headers=cfg.headers or None) as transport:
                tools.extend(await load_mcp_tools(transport=transport, client_name=cfg.name))
        except Exception as e:
            errors.append(f"MCP '{cfg.name}' 加载失败: {type(e).__name__}: {e}")
    return tools, errors
```

### MCP 配置示例：configs/mcp/modelscope-fetch.yaml

```yaml
name: modelscope-fetch
type: streamable_http
url: https://mcp.api-inference.modelscope.net/72a8fb8cc6f646/mcp
headers: {}
enabled: true
```

### runtime 接入（P7 关键）

```python
extra_tools: list = []
if settings.mcp_enabled:
    mcp_tools, mcp_errors = await build_mcp_tools()
    extra_tools.extend(mcp_tools)
    for err in mcp_errors:
        logger.warning("[mcp] %s", err)
# create_deep_agent(..., tools=extra_tools or None, ...)
```

### 关于 FastMCP 的判断

FastMCP 是 **MCP server 侧**框架（把能力封装成 MCP 暴露出去）；接入外部 MCP 到 Agent 是
**client 侧**，用 `mcp` SDK + `langchain-mcp-adapters`。P7 采用后者；若未来把 Harness
能力开放成 MCP server 供外部调用，才用 FastMCP。

---

## 十、P8 人工介入 + 安全边界

### 三层 Guardrail：app/agent/guardrails.py

```python
import re

INPUT_FORBIDDEN_PATTERNS = [
    r"ignore\s+(all\s+)?previous\s+instructions", r"override\s+(your\s+)?system\s+prompt",
    r"jailbreak", r"\brm\s+-rf\b", r"\bformat\s+c[:\s]",
]
OUTPUT_SENSITIVE_PATTERNS = [
    r"(sk-[A-Za-z0-9]{20,})", r"(-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----)",
    r"(password(?:\s*[:=]\s*)\S+)", r"(api[_-]?key(?:\s*[:=]\s*)\S+)",
]


class GuardrailResult:
    __slots__ = ("ok", "reason", "level")
    def __init__(self, ok, reason="", level="deny"):
        self.ok, self.reason, self.level = ok, reason, level


def check_input(msg):        # 输入层：拦截越权/恶意指令
    return GuardrailResult(False, f"输入含被拦截的指令模式") if re.search(
        "|".join(INPUT_FORBIDDEN_PATTERNS), msg or "", re.I) else GuardrailResult(True)


def check_action(tool_name, tool_args):   # 步骤层：工具调用前检查
    if tool_name in ("write_file", "edit_file", "delete"):
        path = str(tool_args.get("file_path", ""))
        if path and not path.startswith("/"):
            return GuardrailResult(False, f"文件操作需绝对路径: {path}")
    return GuardrailResult(True)


def check_output(out):       # 输出层：拦截敏感信息外泄
    if re.search("|".join(OUTPUT_SENSITIVE_PATTERNS), out or "", re.I):
        return GuardrailResult(False, "输出疑似含敏感信息")
    return GuardrailResult(True)
```

### 审批核心：app/agent/approvals.py

```python
from langgraph.types import Command


async def read_pending_approval(agent, config):
    """读取该会话当前待审批中断信息。"""
    state = await agent.aget_state(config)
    interrupts = getattr(state, "interrupts", None) or []
    if not interrupts:
        return {"pending": False}
    value = interrupts[0].value if hasattr(interrupts[0], "value") else interrupts[0]
    actions = []
    if isinstance(value, dict):
        for ar in value.get("action_requests", []):
            if isinstance(ar, dict):
                actions.append({
                    "type": ar.get("type", "tool_call"),
                    "tool": ar.get("tool") or (ar.get("tool_call") or {}).get("name", ""),
                    "args": ar.get("args") or (ar.get("tool_call") or {}).get("args", {}),
                })
    return {"thread_id": config.get("configurable", {}).get("thread_id"),
            "pending": True, "actions": actions}


async def decide(agent, config, decision):
    """对挂起中断做决策（approve/reject）并恢复执行。"""
    return await agent.ainvoke(Command(resume={"decisions": decision}), config)


def build_decision(actions, approve):
    count = len(actions) if actions else 1
    return [{"type": "approve" if approve else "reject"} for _ in range(count)]
```

### 审批 API：app/api/routes/approvals.py

```python
@router.get("/{session_id}", response_model=ApprovalCheckResponse)
async def check_approval(session_id: str):
    agent = await runtime.get_agent()
    info = await approvals.read_pending_approval(agent, {"configurable": {"thread_id": session_id}})
    return ApprovalCheckResponse(session_id=session_id, pending=info.get("pending", False),
                                 actions=info.get("actions") or [])


@router.post("/{session_id}", response_model=ApprovalDecideResponse)
async def decide(session_id: str, req: ApprovalDecideRequest):
    agent = await runtime.get_agent()
    info = await approvals.read_pending_approval(agent, {"configurable": {"thread_id": session_id}})
    if not info.get("pending"):
        raise HTTPException(status_code=409, detail="当前无待审批项")
    approve = req.decision == "approve"
    decisions = approvals.build_decision(info.get("actions"), approve=approve)
    result = await approvals.decide(agent, {"configurable": {"thread_id": session_id}}, decisions)
    return ApprovalDecideResponse(session_id=session_id, decided=req.decision,
                                  reply=str(result["messages"][-1].content)[:2000])
```

### 审批机制（deepagents HITL，经实测验证）

- `create_deep_agent(permissions=[FilesystemPermission(mode="interrupt", paths=["/system/**", ...])])`
  → deepagents 自动调 `_build_interrupt_on_from_permissions` 生成 interrupt_on，
  自动挂 `HumanInTheLoopMiddleware`。
- 模型调用匹配路径的工具 → `interrupt()` 抛 GraphInterrupt → 运行暂停（checkpoint pending）。
- 审批：`read_pending_approval` 读待审批 → `decide` 用 `Command(resume={"decisions":[...]})` 恢复。
- 关键坑：权限路径须用 **glob 通配**（`/system/**`），`/system/` 不匹配子孙路径。

### 端到端验证结果（P8 实测）

```
模型写 /system/config.txt → 触发中断 → pending=True
read_pending_approval → actions=[{file_path:/system/config.txt, content:abc}]
decide(approve) → 模型确认 "工具返回成功（Updated file /system/config.txt）"
```

---

## 十一、P9 Vue3 前端 + SSE 流式输出

### 后端 SSE：app/api/routes/chat_stream.py

```python
import json
from fastapi import APIRouter, HTTPException
from fastapi.responses import StreamingResponse
from app.agent import runtime
from app.schemas.chat import ChatRequest

router = APIRouter(prefix="/api/v1", tags=["chat-stream"])


async def _event_stream(session_id: str, message: str):
    def sse(obj: dict) -> str:
        return f"data: {json.dumps(obj, ensure_ascii=False)}\n\n"

    yield sse({"type": "session", "session_id": session_id})   # 先推送会话 ID
    try:
        async for kind, data in runtime.stream_chat(session_id, message):
            if kind == "token":
                yield sse({"type": "token", "content": data})
            elif kind == "tool":
                yield sse({"type": "tool", "name": data})
            elif kind == "approval":
                yield sse({"type": "approval", "pending": bool(data.get("pending")),
                           "actions": data.get("actions") or []})
            elif kind == "result":
                yield sse({"type": "result", "content": data})
        yield "event: done\ndata: {}\n\n"
    except Exception as e:
        yield sse({"type": "error", "detail": f"{type(e).__name__}: {e}"})


@router.post("/chat/stream")
async def chat_stream(req: ChatRequest):
    if req.session_id is None:
        session_id = runtime.create_session()
    elif runtime.session_exists(req.session_id):
        session_id = req.session_id
    else:
        raise HTTPException(status_code=404, detail="session not found")
    return StreamingResponse(_event_stream(session_id, req.message),
                             media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache",
                                      "Connection": "keep-alive",
                                      "X-Accel-Buffering": "no"})
```

### 后端流式生成器：runtime.stream_chat

```python
async def stream_chat(session_id: str, user_message: str):
    """产出 (kind, data) 事件序列：token/tool/approval/result/error。"""
    from app.agent.approvals import read_pending_approval

    agent = await get_agent()
    config = {"configurable": {"thread_id": session_id}}

    async for chunk in agent.astream(
        {"messages": [{"role": "user", "content": user_message}]},
        config, stream_mode="messages",
    ):
        msg, metadata = chunk
        node = metadata.get("langgraph_node")
        content = getattr(msg, "content", "")
        if node == "tools":
            yield ("tool", str(getattr(msg, "name", "")))
        if isinstance(content, str) and content:
            yield ("token", content)

    try:
        pinfo = await read_pending_approval(agent, config)
        if pinfo.get("pending"):
            yield ("approval", pinfo)
            return
    except Exception:
        pass

    try:
        state = await agent.aget_state(config)
        messages = (state.values or {}).get("messages") or []
        yield ("result", str(messages[-1].content) if messages else "")
    except Exception:
        yield ("result", "")
```

### 前端 SSE 消费：frontend/src/sse.js

```javascript
export async function streamChat(message, sessionId, signal, onEvent) {
  const body = sessionId && sessionId !== 'new' ? { message, session_id: sessionId } : { message }
  const resp = await fetch('/api/v1/chat/stream', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(body),
    signal,
  })
  if (!resp.ok) throw new Error(`HTTP ${resp.status}`)

  const reader = resp.body.getReader()
  const decoder = new TextDecoder('utf-8')
  let buffer = ''
  while (true) {
    const { done, value } = await reader.read()
    if (done) break
    buffer += decoder.decode(value, { stream: true })
    const chunks = buffer.split('\n\n')
    buffer = chunks.pop()
    for (const chunk of chunks) parseChunk(chunk, onEvent)
  }
  if (buffer.trim()) parseChunk(buffer, onEvent)
}

function parseChunk(chunk, onEvent) {
  let dataLine = null
  for (const line of chunk.split('\n')) {
    if (line.startsWith('data:')) dataLine = line.slice(5).trim()
  }
  if (!dataLine) return
  let parsed = null
  try { parsed = JSON.parse(dataLine) } catch { parsed = { raw: dataLine } }
  onEvent(parsed)
}
```

### 前端对话界面：frontend/src/App.vue（关键逻辑）

```javascript
// setup() 中
async function send() {
  const text = input.value.trim()
  if (!text || streaming.value) return
  input.value = ''
  const controller = new AbortController()
  activeController.value = controller

  messages.push({ role: 'user', content: text })
  const assistantMsg = { role: 'assistant', content: '' }
  messages.push(assistantMsg)
  streaming.value = true

  await streamChat(text, sessionId.value, controller.signal, (evt) => {
    if (evt.type === 'session') sessionId.value = evt.session_id
    else if (evt.type === 'token') assistantMsg.content += evt.content
    else if (evt.type === 'tool') assistantMsg.content += `[调用工具: ${evt.name}] `
    else if (evt.type === 'approval') approval.value = { ...evt }
    else if (evt.type === 'result') { if (evt.content) assistantMsg.content = evt.content }
    else if (evt.type === 'error') assistantMsg.content = `⚠️ ${evt.detail}`
  })
  streaming.value = false
}

async function decide(approve) {
  const decision = approve ? 'approve' : 'reject'
  const resp = await fetch(`/api/v1/approvals/${sessionId.value}`, {
    method: 'POST', headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ decision }),
  })
  const data = await resp.json()
  if (!resp.ok) throw new Error(data.detail || '审批失败')
  messages.push({ role: 'assistant', content: `[已${approve ? '批准' : '拒绝'}] ${data.reply}` })
  approval.value = null
}
```

### 前端运行（当前沙箱禁止 Node spawn，需在普通终端）

```powershell
cd D:\Deepseek_Harness\agent-harness\frontend
npm install
npm run dev        # http://127.0.0.1:5173（vite 代理 /api → 127.0.0.1:8000）
```

---

## 十二、P10 部署配置

### 生产/日常启动后端

```powershell
cd D:\Deepseek_Harness\agent-harness\backend
C:\Users\Administrator\anaconda3\envs\agent_env\python.exe -m uvicorn app.main:app --host 0.0.0.0 --port 8000
```

### 环境变量（.env）

```
DEEPSEEK_API_KEY=sk-***
DEEPSEEK_BASE_URL=https://api.deepseek.com
DEEPSEEK_MODEL=deepseek-v4-flash
MYSQL_URL=mysql+pymysql://root:CHANGE_ME@127.0.0.1:3306/agent_harness?charset=utf8mb4
CONTEXT_MAX_MESSAGES=80
CONTEXT_KEEP_MESSAGES=24
MCP_ENABLED=false
APP_ENV=development
```

### 依赖与数据库前提

- Python 环境：conda `agent_env`（Python 3.12）；`pip install -r requirements-dev.txt`。
- MySQL 8.0 运行中，`agent_harness` 库存在（应用启动自动建表）。
- SQLite：`backend/data/harness.sqlite`（消息历史，自动创建）。
- 前端：`npm install`（Node 24+），`npm run dev`（开发）/ `npm run build`（生产静态产物）。

### 测试

```powershell
cd D:\Deepseek_Harness\agent-harness\backend
C:\Users\Administrator\anaconda3\envs\agent_env\python.exe -m pytest -c pytest.ini tests -p no:cacheprovider -q
# 当前全套 51 passed，~13s
```

---

## 十三、P11 本地磁盘读写 + OpenSandbox 执行（判断路由）

> 详细设计见 `docs/P11-opensandbox-hybrid-design.md`。核心：**不需要执行代码时用本地磁盘
> 读写；需要执行代码时进 OpenSandbox 沙箱，结果回给大模型**。

### 混合后端：app/agent/opensandbox_backend.py

```python
class PathMapper:
    """虚拟路径(/workspace/a.txt) <-> 沙箱真实路径(/workspace/a.txt)。"""
    def to_sandbox(self, virtual_path: str) -> str: ...
    def to_virtual(self, sandbox_path: str) -> str: ...


def translate_command(command: str, root: str) -> str:
    """把命令里的虚拟绝对路径改写成容器路径（/usr、/bin 等系统目录不动）。"""


class SandboxRegistry:
    """每会话一个沙箱容器；懒创建、is_healthy() 探活、TTL 到期自动重建、atexit 统一销毁。"""
    def get(self, session_key: str, path_prefix: str | None = None) -> _SandboxEntry: ...
    def destroy(self, session_key: str) -> None: ...


class OpenSandboxBackend(SandboxBackendProtocol):
    """OpenSandbox 容器 → deepagents 执行后端。只实现必需的四个方法。"""
    @property
    def id(self) -> str: ...
    def execute(self, command, *, timeout=None) -> ExecuteResponse: ...   # SDK commands.run
    def upload_files(self, files) -> list[FileUploadResponse]: ...        # SDK files.write_file
    def download_files(self, paths) -> list[FileDownloadResponse]: ...    # SDK files.read_bytes
    def list_files(self) -> dict[str, tuple[float, int]]: ...             # find 出整树清单


class HybridDefaultBackend(FilesystemBackend, SandboxBackendProtocol):
    """CompositeBackend 的 default：文件→本地磁盘，execute→沙箱，执行前后自动同步。"""
    def execute(self, command, *, timeout=None) -> ExecuteResponse:
        # 1) push 本地改动 → 容器   2) 拍容器快照
        # 3) 沙箱执行               4) pull 容器产出 → 本地磁盘
        ...
    def push_local_changes(self) -> list[str]: ...     # 本地指纹增量推送
    def pull_sandbox_changes(self, remote_before=None) -> list[str]: ...


def build_hybrid_backend(*, session_key, root_dir=None, routes=None,
                         prefix=DEFAULT_PREFIX, sync_before_execute=True,
                         sync_after_execute=True) -> CompositeBackend: ...
```

### 对外组装：app/agent/sandbox.py（P3 起沿用同一组函数名）

```python
def build_main_backend(session_key="default", workspace=None):
    """CompositeBackend(default=混合后端, routes={"/workspace/": default})。
    routes 仅用于通过权限 route 校验，读写位置不变。"""
    ...

def default_permissions():
    """写 /workspace/** 需人工审批（mode="interrupt"）。"""
    ...

def execute_allowed() -> bool:      # 跟随 SANDBOX_ENABLED
def build_store_backend():          # 备用：纯内存虚拟 FS（P3 语义）
def build_exec_backend(session_key, path_prefix=None)   # 单独拿沙箱连接做诊断
```

### runtime.py：按会话缓存 agent

```python
_agents: dict[str, tuple[agent, backend]] = {}

async def get_agent(session_key="default"):   # 每个会话一个 agent + 一个沙箱容器
def get_main_backend(session_key="default")   # 供文件浏览/下载 API 使用
```

### 三个决定性的框架事实（deepagents 0.7.1）

- `CompositeBackend` 的**文件操作按路径前缀路由**，未命中走 `default`；
- `CompositeBackend.execute()` **不按路径路由，永远委托给 `default`**；
- `supports_execution()` 对 `CompositeBackend` 判断 `isinstance(backend.default, SandboxBackendProtocol)`
  —— 为真才注册 `execute` 工具。

因此 `default` 必须**同时**是「本地磁盘文件后端」和「可执行后端」，
继承顺序为 `class HybridDefaultBackend(FilesystemBackend, SandboxBackendProtocol)`。

### 权限约束（实测，两个坑）

1. backend 一具备执行能力，`FilesystemMiddleware` 就要求 `permissions` 里**所有路径**都落在
   某个 route 前缀内（`_all_paths_scoped_to_routes`），否则 `NotImplementedError`。
   故 `build_main_backend()` 声明一条指向自身实例的占位 route `"/workspace/"`。
2. **子智能体同样受这条约束**：`create_deep_agent` 会为每个 subagent 单独构造
   `FilesystemMiddleware`，用的是**父级 backend**（`graph.py:668`），权限没写就继承父级。
   所以 `configs/subagents/*.yaml` 里的路径必须 route 化 —— `build_subagent()` 用
   `_to_route_path()` 把工作区相对路径补成带前缀的 glob：
   `/` → `/workspace/**`，`/notes/` → `/workspace/notes/**`。
   否则报："FilesystemMiddleware does not yet support permissions with backends that
   provide command execution"，且**只在有子智能体时才暴露**。

另外 `runtime.get_agent()` 会先跑 `sandbox.validate_permissions()`：把主 Agent 与全部子智能体
的权限路径一起核一遍，有问题抛一条人能看懂的错误。

### 验证

```bash
cd D:\Deepseek_Harness\agent-harness\backend
python -m pytest tests/test_p11_hybrid_backend.py -o addopts="" -p no:cacheprovider -q   # 11 passed
python ..\_probe\hybrid_backend_probe.py        # 独立探针（路由 + 同步闭环）
python ..\_probe\agent_wiring_probe.py          # create_deep_agent 工具注册（含 execute）
```

`create_deep_agent` 实测注册的工具：
`['delete','edit_file','execute','glob','grep','ls','read_file','task','write_file']`

---

## 十四、链路图（最终版）

```
用户请求（前端 Vue3 对话界面）
   │ POST /api/v1/chat/stream  (SSE)
   ▼
FastAPI  chat_stream 路由
   │ runtime.stream_chat() → agent.astream(stream_mode="messages")
   ▼
DeepAgents 主 Agent（LangGraph）
   ├─ 中间件管道：Summarization(上下文压缩) / Logging / Audit→MySQL / RateLimit
   ├─ SkillsMiddleware：/skills/ 技能（YAML→SKILL.md 播种）
   ├─ FilesystemMiddleware：8 个工具按能力分流
   │    ├─ ls/read_file/write_file/edit_file/delete/glob/grep → FilesystemBackend → 本地磁盘
   │    └─ execute → OpenSandboxBackend → OpenSandbox 容器（执行前后自动同步文件）
   ├─ permissions(mode=interrupt)：写 /workspace/** → 人工审批中断
   ├─ subagents：task 工具委派子智能体（隔离上下文，同一模型）
   └─ tools：MCP 工具（MCP_ENABLED 时，可插拔）+ tavily_search
   │
   ├─ checkpointer(AsyncSqliteSaver) → SQLite 消息历史（跨进程持久）
   ├─ store(AsyncSqliteStore) → SQLite 短期记忆
   └─ MySQL：sessions / audit_logs（业务数据）
   │
   ├─ 审批中断 → approvals API → 前端审批弹窗 → approve/reject → Command(resume) 恢复
   ▼
SSE 事件流（token/tool/approval/result）→ 前端逐字显示 + 审批交互
```

### 存储分层总结

| 层 | 存储 | 生命周期 | 内容 |
|----|------|---------|------|
| 消息历史 | SQLite checkpoint | 会话级、跨进程持久 | BaseMessage 全量 |
| 短期记忆 | SQLite store | 会话级 | 当前任务进度/临时上下文 |
| 业务数据 | MySQL | 持久 | sessions / audit_logs（后续 skills/subagents/mcp/长期记忆）|
| 工作区文件 | **本地磁盘** `<repo>/workspace` | 持久（用户可见、可下载）| read_file/write_file/edit_file 读写的文件 |
| 执行环境 | **OpenSandbox 容器** | 会话级（TTL 到期回收）| execute 跑命令的算力，产出同步回本地磁盘 |
| 备用虚拟 FS | InMemoryStore（`build_store_backend()`）| 进程内 | 不碰宿主磁盘的部署形态 |

