# P18 阶段 0：用户认证隔离 + 多供应商 LLM —— 技术方案与改动清单

> 状态：**待确认**（确认后按 §9 分阶段实施）
> 依据：`认证隔离与多供应商接入-提示词.md`
> 结论摘要：整体可行，但有 **3 处现有设计与需求冲突**必须在动手前定案（§3），
> 另有 **5 项依赖已就绪**使工作量比预期小（§7）。

---

## 1. 先复述：我对现有架构的理解

按提示词 §八 的要求，先说明我对将被动到的几处核心机制的理解，供你校验我没读错。

### 1.1 运行与连接解耦（`app/agent/runner.py`）

`run_manager` 是**进程内单例**，`_runs: dict[session_id, SessionRun]`。

- `start(session_id, agent_input, user_message)` 用 `asyncio.ensure_future` 起一个**不属于任何请求**的后台 task（`_drive`）；
- `_publish` 同时做两件事：广播给 `run.subscribers`（细粒度事件）、并入 `run.events` 回放缓冲（相邻同类增量**合并**，见 `_DELTA_FIELDS`）；
- `subscribe(session_id, since=None)` 先回放快照、再接直播；`since` 是"只看本次增量"的用法，审批恢复走它（避免把审批前 4000+ 条事件重播一遍）；
- 结束状态有三种非 running：`done` / `error` / `stopped`，另有 `awaiting_approval`（判据是缓冲最后一条是 `approval` —— `_drive` 的 else 分支）；
- 一轮结束后缓冲保留 `KEEP_SECONDS=180` 秒。

**关键含义**：运行不持有请求上下文，所以"当前用户"这个概念在 `_drive` 里**根本不存在**。需求 §3.3.4 要求"start 时捕获并绑定 user_id"——在这个架构下它的实际作用是**给订阅侧和停止侧提供校验依据**，而不是给后台 task 传递身份。我会把 `user_id` 存成 `SessionRun` 的字段。

### 1.2 agent 缓存（`app/agent/runtime.py`）

`_agents: dict[session_key, (agent, backend)]`，键 = `session_id`（uuid4 hex，全局唯一）。

- `get_agent(session_key)` 懒加载：`build_main_backend(session_key)` → `validate_permissions()` → `compile_all_skills()` + `seed_skills()` → 组装中间件 → `create_deep_agent(...)`；
- `invalidate_agents(session_key=None)`：传 None 时**清空全部**；
- **权限是编译期输入**：`permissions` / `interrupt_on` 变化必须重建 graph，这就是 HITL 热插拔的实现方式。

**关键含义**：模型（`model=build_llm()`）和 backend（工作区根 + 沙箱容器）都是**构建期绑定的**。所以"切换供应商"必须 `invalidate_agents(session_id)` 重建——需求 §4.2.4 的判断是对的，我会沿用。

### 1.3 checkpointer / store（`app/agent/context.py`）

- `AsyncSqliteSaver` + `AsyncSqliteStore` 共享**一个** `aiosqlite.Connection`，进程内单例，绑定首次调用所在的 event loop；
- `thread_id == session_id`；
- `memory_sources()` 走的是**文件通道**（`/workspace/Agent.md`），不是 store；
- lifespan `finally` 里 `await context.aclose()`（不关会让解释器退不出去 → `--reload` 假死）。

### 1.4 SSE 事件契约（`app/agent/runner.py: to_sse_event`）

`runtime.agent_event_stream` 产出 `(kind, data)`，由 `to_sse_event` 映射成前端事件字典，`api/routes/chat_stream.py` 只做 `data: {...}\n\n` 序列化。

事件类型：`session` / `token` / `thinking` / `tool_call` / `tool_result` / `subagent_start` / `subagent_token` / `subagent_thinking` / `subagent_tool` / `subagent_end` / `subagent_usage` / `usage` / `approval` / `stopped` / `result` / `error` / `done`（外加审批流的 `decision`、接回流的 `resumed`）。

**本期不改这个契约**（§五.1），只在 `session` 事件/端点里追加字段。

---

## 2. 现状核对：与提示词描述一致之处

| 提示词说 | 实际 | 结论 |
|---|---|---|
| `build_llm()` 直接返回 `ChatDeepSeek` | ✔ `app/agent/model.py`，全局唯一 LLM 构造点 | 一致 |
| `_agents` 按会话缓存、`get_agent` 懒加载 | ✔ | 一致 |
| 三张表均无 `user_id` | ✔ `sessions`(87) / `tool_call_logs`(761) / `audit_logs`(188) | 一致，**存量数据真实存在** |
| 路由全部无鉴权，`GET /sessions` 返回全表 | ✔ `db.query(DbSession).order_by(...).all()` | 一致 |
| 无 alembic，lifespan 里 `create_all` | ✔ | 一致 |
| CORS `allow_origins=["*"]` | ✔ `main.py:46` | 一致 |
| 缺 `langchain-anthropic` / JWT / 密码哈希 / 加密库 | ✘ **都已安装**（见 §7） | 工作量减少 |
| `load_all_subagents()` 子智能体同模型 | ✔ 且**不传 model 即继承主 Agent** | 改动量比预期小 |

---

## 3. ⚠️ 三处冲突 / 必须澄清点（请重点看这一节）

### 冲突 1：本地工作区**不是**按会话隔离的，`files.py` 连会话都不知道

**事实**：

```python
# runtime.py:63
backend = build_main_backend(session_key=session_key)
# sandbox.py:130 → build_hybrid_backend(root_dir=None, ...)
# opensandbox_backend.py:1048
root = Path(root_dir) if root_dir is not None else local_workspace_root()   # <repo>/workspace
```

`session_key` 只决定 **OpenSandbox 容器**（`registry.get(session_key)`），**不决定本地磁盘根**。所有会话共用 `<repo>/workspace/`：

```
workspace/
  Agent.md                                  ← 长期记忆，全用户共享
  LeetCode_Hot100_Python题解.md
  hot100_build/  skills/  tmp/  ...
```

`files.py` 更彻底：4 个端点全部调 `get_main_backend()`（无参 → `session_key="default"`）。

**含义**：需求 §3.3.1 要求"文件浏览/下载先校验该 session 属于当前用户"，在这个架构下**语义不成立**——文件本来是跨会话共享的。而 §3.3.6 的"长期记忆隔离"就是同一个根因：`Agent.md` 在共享工作区里，**A 的偏好会直接进 B 的系统提示词**（`prompt.memory_file_path()` → `local_workspace_root()/Agent.md`）。

**我的建议：隔离粒度定在「用户」，不是「用户+会话」。**

| 方案 | 工作区路径 | 评价 |
|---|---|---|
| **A. 按用户（推荐）** | `workspace/<user_id>/` | 同一用户跨会话共享产物（符合"我的工作台"直觉）；`Agent.md` 天然按用户隔离，且**保住"跨会话长期记忆"这个设计目标**（`prompt.py` 模块头明确写的） |
| B. 按用户+会话 | `workspace/<user_id>/<session_id>/` | 更严格，但 `Agent.md` 变成每会话一份 → **长期记忆失去跨会话意义**，直接破坏现有设计意图；且用户在会话 B 里找不到会话 A 刚写的文件 |

选 A 后，`files.py` 的语义修正为：**文件 API 按当前登录用户的工作区服务，不接受任何能指向他人目录的参数**。验收 §7.3（u2 读不到 u1 的文件）由 `user_id` 物理隔离满足，比"检查 session 归属"更强。

> ### ✅ 已定案：**A. 按用户** → `workspace/<user_id>/`

### 冲突 2：P12 本机磁盘挂载 `/mnt/*` 是**进程级全局**的

你的 `.env` 现在是 `HARNESS_MACHINE_ACCESS=true` + `HARNESS_MACHINE_MOUNTS=` 留空 ⇒ 自动挂载 `c=C:/`、`d=D:/`……

`build_machine_routes()` 返回的 route 表是**全局配置的产物**，所有会话共用。多租户下这等于：**任何注册用户都能让 agent 读写整台服务器/这台电脑的所有文件**。这是本期最大的越权面，比会话隔离严重得多。

**备选**：仅 `role=admin` 生效（我原建议）、全局关闭（最安全）、用户级挂载配置（本期过重）。

> ### ✅ 已定案：**保持全局开放**（你已确认）
>
> 即所有登录用户都能让 agent 读写本机磁盘。**这是一个显式接受的风险**，我如实记录后果：
> - 注册用户 A 可以让 agent `read_file /mnt/d/...` 读到这台电脑上任何文件（包括本仓库源码、
>   `backend/.env` 里的 `DEEPSEEK_API_KEY` / `SANDBOX_API_KEY` / 迁移后新增的 `JWT_SECRET` / `FERNET_KEY`）。
> - 这不是"跨用户隔离"能挡住的：`/mnt/*` 是**机器级**资源，绕过工作区隔离。
> - **因此本期的隔离验收（§7.3）只覆盖「用户在 Harness 内的数据」**——会话、工作区文件、
>   长期记忆、审批、运行流。`/mnt/*` 不在隔离范围内，这是你要的语义。
> - 实施时我会：① 保留 `HARNESS_MACHINE_ACCESS` 作为**唯一收紧开关**，将来要收只需改一个判定点；
>   ② 提示词里的挂载段落加一句"这是本机共享磁盘，不是你的私有空间"，让模型不把它当用户工作区；
>   ③ 在 `docs/P18` 与 `README` 里写明该风险，避免以后忘了。
> - ⚠️ 如果这套部署将来会开放给**本机以外的人**注册，请回来收紧这一项。

### 冲突 3：HITL 开关与"挂起中断登记表"是全局的

- `permissions._global_hitl` + `_session_hitl`：`PUT /api/v1/hitl/config` 不带 `session_id` 时改**全局**默认，并 `invalidate_agents(None)` 清空**所有用户**的 agent 缓存；
- `approvals._pending_sessions: set[str]`：`clear_pending_interrupt(None)` 会把**所有人**挂起的审批用 reject 收尾。

**含义**：普通用户点一下"关闭人工介入"，会 reject 掉其他用户正在等的审批。这不是安全问题（不泄漏数据），但是明确的跨用户干扰。

**我的建议（未定案）**：全局开关降为 admin 专属；普通用户的 `PUT /hitl/config` 只允许带自己的 `session_id`；`clear_pending_interrupt` 的目标集合按 `user_id` 过滤。成本很低，且堵住的是"普通用户点一下会 reject 掉别人正在等的审批"这个真实干扰。如果你希望普通用户也能改全局开关，说一声我改回原状。

---

## 4. 目标一：认证与多租户隔离

### 4.1 数据模型

**新增 `users`**

| 列 | 类型 | 说明 |
|---|---|---|
| `id` | `String(32)` PK | uuid4 hex，与 `sessions.id` 同风格 |
| `email` | `String(255)` unique, index | 登录名（按 §3.1.1 建议取 email） |
| `password_hash` | `String(255)` | argon2id（见 §7） |
| `display_name` | `String(128)` | 展示名 |
| `role` | `String(16)` default `user` | `user` / `admin`（预留） |
| `is_active` | `Boolean` default True | 禁用后无法登录 |
| `created_at` / `updated_at` | `DateTime` | 与现有风格一致（`server_default=func.now()`） |

**改 `sessions`**：`user_id String(32) NOT NULL index`；`provider_id String(32) NULL index`（会话记住模型选择）。

**改 `tool_call_logs` / `audit_logs`**：`user_id String(32) NULL index`。
- 为什么**可空**：存量里存在孤儿行（会话已删）。迁移按 `session_id → sessions.user_id` join 回填，回填不到的留 NULL 并在迁移日志里报数。新写入的行一律带 `user_id`（`_log_tool_event` 增加参数）。

**新增 `llm_providers`**：见 §5.1。

### 4.2 认证机制

- `app/core/security.py`：
  - `hash_password(p) / verify_password(p, h) / needs_rehash(h)` → **argon2-cffi**（已装，见 §7）；
  - `create_access_token(user_id, expires_minutes)` / `decode_token(token)` → **PyJWT HS256**，payload = `{sub, iat, exp, role}`。
- `JWT_SECRET` 来自 `.env`。**未配置时**：`APP_ENV=development` 自动生成 + `logger.warning`；非 development **拒绝启动**（避免默认密钥上线）。比提示词的"固定默认值"更严一点，理由是默认密钥一旦上线等于没有认证。
- 端点：
  - `POST /api/v1/auth/register` → 受 `ALLOW_REGISTRATION` 控制（默认 true），默认角色 `user`；
  - `POST /api/v1/auth/login` → `{access_token, token_type, expires_in, user}`；
  - `GET  /api/v1/auth/me`；
  - 本期**不做 refresh token**（access 过期时间可配，默认 24h），理由：无 vue-router 的单页应用 + localStorage 方案下 refresh 的收益不足以抵消复杂度。如你要，可以加，我按需实现。
- 依赖：
  - `get_current_user`：解析 `Authorization: Bearer`，无效/过期/用户禁用 → **401** `{"detail": "..."}`（与现有 `HTTPException` 风格一致）；
  - `get_current_admin`；
  - `get_owned_session(session_id, db, user) -> DbSession`：不存在**或** `user_id != user.id` → **404**（按 §3.3.2，不泄漏资源存在性）。
- **默认拒绝（白名单制）**：在 `api/routes/__init__.py` 给聚合 router 挂 router 级依赖：

  ```python
  api_router = APIRouter(dependencies=[Depends(get_current_user)])
  ```

  `auth` router **不经过** `api_router`，在 `main.py` 里单独 `include_router`。
  ⇒ 今后任何新增业务路由**自动**受保护，不会漏加。

### 4.3 隔离落点（逐文件清单）

| 文件 | 改动 |
|---|---|
| `app/api/deps.py` **新增** | `get_current_user` / `get_current_admin` / `get_owned_session` |
| `app/api/routes/sessions.py` | `list_sessions` 加 `WHERE user_id = me`；其余 7 个端点先 `get_owned_session`；`run/stream`、`stop` 再叠 `run_manager` 归属校验 |
| `app/api/routes/chat.py` · `chat_stream.py` | 新建会话传 `user_id`；已有会话走归属校验；把 `user_id` 传给 `run_manager.start` |
| `app/api/routes/approvals.py` | `check_approval` / `decide` 先归属校验；`clear_pending_interrupt` 目标按 user 过滤 |
| `app/api/routes/hitl.py` | 全局切换仅 admin；普通用户只能带自己的 `session_id` |
| `app/api/routes/files.py` | 4 个端点改为 **当前用户工作区**（`get_user_backend(user_id)`）；保留 `_assert_deletable` 的 `/workspace/` 前缀 + 拒绝 `/mnt/` 边界 |
| `app/api/routes/skills.py` | `POST /skills/` 收紧为 **admin**（它写的是系统级 YAML，且 §3.3.8 已声明 skills 全局共享） |
| `app/agent/runtime.py` | `create_session(user_id, title, provider_id=None)`；新增 `get_session_owner(session_id)`；`get_agent` 用 owner 解析**工作区根 + 供应商**；`_log_tool_event` 写 `user_id` |
| `app/agent/runner.py` | `SessionRun.user_id`；`start(..., user_id)`；`subscribe/stop/current` 由调用方校验（manager 内也加断言，双保险） |
| `app/agent/permissions.py` | 作用域加 `user_id` 维度 |
| `app/agent/approvals.py` | `_pending_sessions` 由 `set[str]` 改 `dict[session_id, user_id]` |
| `app/agent/prompt.py` | `memory_file_path(user_id)` → 用户工作区下的 `Agent.md` |
| `app/agent/opensandbox_backend.py` | 新增 `user_workspace_root(user_id)`；`build_hybrid_backend` 接受显式 `root_dir` |
| `app/core/workspace.py` **新增** | `workspace_root(user_id=None)` 统一入口（避免各处自己拼路径） |

### 4.4 明确不做的（决策说明）

- **skills / subagents / MCP 保持全局共享**（§3.3.8）。它们是系统级 YAML（`backend/configs/`），本期不做用户级隔离。写接口收紧为 admin 作为补偿。
- **checkpointer 的 SQLite 文件不加密、不做分库**。`thread_id == session_id` 保持不变（§3.1.5），隔离在业务层强制。**如实说明**：任何能直连 `backend/data/harness.sqlite` 的人可以读出所有用户的消息——本期不做静态加密，属于部署边界（文件权限）问题。
- **`AsyncSqliteStore` 不做 namespace 改造**。我核对了实际数据：`store` 表**只有 1 行**，是 P3 时期 `StoreBackend` 演示留下的 `assistant.filesystem/hello.txt`，当前记忆通道走文件（`Agent.md`），业务代码没有写 store。所以"记忆经 store 泄漏"在本项目**不存在**；改造它没有收益。我会在阶段 2 加一个**只读探针**（断言 store 无新写入、无跨用户 key）作为回归证据，而不是做结构性改造。
- **`/mnt/*` 见冲突 2**。

---

## 5. 目标二：OpenAI / Anthropic 双协议多供应商

### 5.1 `llm_providers` 表

| 列 | 类型 | 说明 |
|---|---|---|
| `id` | `String(32)` PK | uuid4 hex |
| `user_id` | `String(32)` NULL index | **NULL = 系统级共享供应商**；非空 = 用户私有 |
| `name` | `String(128)` | 展示名（"我的 DeepSeek"） |
| `protocol` | `String(16)` | `openai` / `anthropic` |
| `base_url` | `String(512)` NULL | 留空用官方默认 |
| `api_key_encrypted` | `Text` | Fernet 密文 |
| `api_key_hint` | `String(32)` | 掩码尾巴（`sk-****1234`），列表展示用**不解密** |
| `model` | `String(128)` | 模型名 |
| `parameters` | `Text` | JSON（temperature / timeout / max_tokens …），空=系统默认 |
| `is_default` | `Boolean` | 每用户至多一个为真；系统级一个全局默认 |
| `enabled` | `Boolean` default True | |
| `created_at` / `updated_at` | `DateTime` | |

- "每用户至多一个默认"用**应用层事务**保证（设默认时先清同 owner 的其它默认），不靠唯一索引（MySQL 无法表达条件唯一）。
- 引导：启动时若**不存在任何系统级供应商**，则用现有 `.env` 的 `DEEPSEEK_API_KEY/BASE_URL/MODEL` 写入一条 `user_id=NULL, is_default=1` 的 openai 协议供应商 ⇒ 老部署无感升级（§7.8 验收）。

### 5.2 模型工厂重构（`app/agent/model.py`）

```python
def build_llm(cfg: ProviderConfig | None = None) -> BaseChatModel
```

| 协议 | 实现 | ⚠️ 实测到的参数名（已在本机核对 langchain-anthropic 1.5.3 / langchain-openai 1.3.2） |
|---|---|---|
| `openai` | `ChatOpenAI` | `openai_api_base`（alias `base_url`）、`openai_api_key`（alias `api_key`）、`request_timeout`（alias `timeout`）、`temperature`、`max_tokens` |
| `openai` + DeepSeek 官方域名 | **`ChatDeepSeek`** | 见下方"关键取舍" |
| `anthropic` | `ChatAnthropic` | `anthropic_api_url`、`anthropic_api_key`、`default_request_timeout`、`max_tokens`（**必填**） |

**三处必须写进代码注释的坑**：

1. **`ChatAnthropic` 没有 `base_url` / `api_key` / `timeout` 字段**，分别是 `anthropic_api_url` / `anthropic_api_key` / `default_request_timeout`。写错不会被 pydantic 拦（`extra` 行为），会静默走默认地址 —— 排查成本极高。
2. **`max_tokens` 对 Anthropic 是必填**（Messages API 要求）。工厂里给默认值（建议 4096），否则第一次调用就 400。
3. **必须保留 `ChatDeepSeek` 特化分支**。本项目依赖 `additional_kwargs.reasoning_content` 来做"思考增量"（工作记忆第 5 条：实测 31.8s 的回答里前 26.9s 只有 reasoning 在动）。`ChatDeepSeek` 是 langchain-deepseek 对该字段的专门解析；换成裸 `ChatOpenAI` 有**丢失思考增量**的风险。
   ⇒ 工厂按 `base_url` 命中 `api.deepseek.com` 时走 `ChatDeepSeek`，其余走 `ChatOpenAI`。同时给 `ProviderConfig` 留一个可选 `dialect`（`auto`/`openai`/`deepseek`）供手工覆盖。
   ⇒ 阶段 4 的**第一条探针**就是：用 DeepSeek 真跑一次，断言 `reasoning_content` 仍能拿到（保住 §7.8 回归）。

**覆盖全部 LLM 构造点**（grep 结果：只有 3 处）：

| 位置 | 现状 | 改法 |
|---|---|---|
| `runtime.py:184` 主模型 | `model=build_llm()` | `model=build_llm(cfg)`，`cfg` 由"会话 provider_id → 用户默认 → 系统默认"解析 |
| `context.py:75` 摘要中间件 | `SummarizationMiddleware(build_llm(), ...)` | 签名改 `build_summary_middleware(model)`，runtime 传**同一个实例** |
| 子智能体 | `load_all_subagents()` **不传 model** → 继承主 Agent | **无需改动**，天然跟随 ✔ |

（`tests/test_p11_hybrid_backend.py:245` 也调 `build_llm()` —— 保持无参可调用即可不破测试。）

**解析顺序与报错**：

```
会话 provider_id → 用户 is_default → 系统级 is_default → ❌
```

落到 ❌ 时抛中文业务异常（不是框架栈），由 API 层转 **400**：
> "尚未配置可用的模型供应商。请到「设置 → 模型供应商」添加，或联系管理员配置系统级供应商。"

**运行期参数容错**：

- 一份已知"不接受某参数"的模型表（`deepseek-reasoner`、`o1`/`o3`/`gpt-5-thinking` 系列 → 剥掉 `temperature`），构造时**主动剔除**而不是等 400；
- 兜底：捕获供应商返回的 4xx，转成中文"该模型不支持参数 X"；
- 文档/提示里标注**"所选模型必须支持 function/tool calling"**（deepagents 强依赖工具调用 + 流式）。

### 5.3 密钥安全（`app/core/crypto.py`）

- `cryptography.Fernet`；主密钥 `FERNET_KEY` 从 `.env` 读；
- `encrypt_secret` / `decrypt_secret` / `mask_secret`（`sk-****1234`，只保留前 3 + 后 4）；
- **明文只在真正发起 LLM 请求时于内存解密**；不进日志、不进审计 `detail`、不进 SSE、不进异常栈（异常里对 key 做一次 scrub）；
- `FERNET_KEY` **缺失时启动即失败**，错误信息里带生成命令：
  `python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"`
  —— 刻意不做"从 JWT_SECRET 派生"的兜底：那会在 `JWT_SECRET` 轮换时**静默毁掉所有已存密钥**（解不开又不报错），代价太大。
- `base_url` 校验：pydantic `AnyHttpUrl`，仅 http/https，拒绝带 userinfo（`http://u:p@host`）。

### 5.4 供应商管理 API（均需登录）

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/api/v1/llm/providers` | 系统级 + 自己的；`api_key` **只回 `api_key_hint`** |
| POST | `/api/v1/llm/providers` | 新增（自己私有） |
| PUT | `/api/v1/llm/providers/{id}` | 改；`api_key` 留空 = 不改动已有密文 |
| DELETE | `/api/v1/llm/providers/{id}` | 删（系统级仅 admin） |
| POST | `/api/v1/llm/providers/{id}/set-default` | 设默认（事务内清同 owner 其它默认） |
| POST | `/api/v1/llm/providers/{id}/test` | 连通性：发一条最小请求（"ping"，max_tokens=1），返回成功/失败 + **脱敏**原因 |
| POST | `/api/v1/llm/providers/test` | **临时配置**测试（编辑时先测后存，不落库） |
| GET | `/api/v1/llm/providers/{id}/models` | 拉 `/v1/models`；失败不报错，返回空 + 提示手填 |

`test` 的错误归一：401→"API Key 无效"、404→"base_url 不可达/路径不对"、模型不存在→"模型 X 不存在"、超时→"连接超时"。
**鉴权与归属**：系统级供应商普通用户**只读**；有 `user_id` 的只有 owner 能改。

### 5.5 会话与发消息的模型选择

- `PATCH /api/v1/sessions/{id}` body 扩展为 `{title?, provider_id?}`（保持向后兼容：只传 title 时行为不变）；
- `POST /api/v1/chat/stream`（与 `/chat`）body 加可选 `provider_id`，表示**本轮切换**：写回 `sessions.provider_id` + `invalidate_agents(session_id)` 再跑本轮；
- 前端模型选择器默认选中"会话 provider → 用户默认 → 系统默认"。

---

## 6. 接口清单汇总

**新增**

```
POST   /api/v1/auth/register
POST   /api/v1/auth/login
GET    /api/v1/auth/me
GET    /api/v1/llm/providers
POST   /api/v1/llm/providers
PUT    /api/v1/llm/providers/{id}
DELETE /api/v1/llm/providers/{id}
POST   /api/v1/llm/providers/{id}/set-default
POST   /api/v1/llm/providers/{id}/test
POST   /api/v1/llm/providers/test
GET    /api/v1/llm/providers/{id}/models
GET    /api/v1/llm/providers/available        # 供前端选择器（脱敏，轻量）
```

**语义变化（不改路径）**

| 端点 | 变化 |
|---|---|
| `GET /api/v1/sessions` | 只返回当前用户的会话 |
| `GET/PATCH/DELETE /api/v1/sessions/{id}`、`/messages`、`/run`、`/run/stream`、`/stop` | 归属校验（越权→404） |
| `POST /api/v1/chat/stream`、`/api/v1/chat` | 新建时绑 `user_id`；已有会话归属校验；body 加 `provider_id` |
| `GET/POST /api/v1/approvals/{id}` | 归属校验 |
| `GET/PUT /api/v1/hitl/config` | 全局切换仅 admin |
| `GET/DELETE /api/v1/files`、`/download` | 按**当前用户工作区**服务 |
| `POST /api/v1/skills/` | 收紧为 admin |
| `/health`、`/`、`/docs` | 保持不鉴权 |

**SSE 契约**：事件类型与字段**不变**。仅在 `{"type":"session"}` 里追加 `provider_id`（前端可选消费，旧前端不受影响）。

---

## 7. 依赖与迁移方案

### 7.1 依赖：比提示词预期少很多

本机 `agent_env` (Python 3.12.13) 实测：

| 包 | 状态 |
|---|---|
| `langchain-anthropic` **1.5.3** | ✅ 已装（提示词说"缺"，实际有） |
| `PyJWT` **2.13.0** | ✅ 已装 |
| `cryptography` **49.0.0** | ✅ 已装（Fernet 可用） |
| `argon2-cffi` **25.1.0** | ✅ 已装 → **密码哈希不必新增依赖** |
| `langchain-openai` 1.3.2 / `langchain-deepseek` 1.1.0 / `deepagents` 0.7.1 | ✅ 已装 |
| `alembic` | ❌ **未装**（若选方案 A 需新增） |
| `bcrypt` / `passlib` | ❌ 未装（选 argon2 则不需要） |

**建议：密码哈希用 argon2-cffi 而非 bcrypt** —— 已装、无版本兼容坑（提示词自己也提醒了"注意锁定兼容的 bcrypt 版本"）、且是当前的推荐算法。这样**唯一可能的新增依赖是 alembic**。

`requirements.txt` 会补齐实际在用但没声明的包（当前它不可信：缺 `pymysql`/`aiosqlite`/`pyyaml`/`langchain-tavily`/`opensandbox`，多 `psycopg2-binary`）——这是顺手的债务清理，不影响功能。

### 7.2 迁移：两个方案（⚠️ 需你定案）

**方案 A：引入 alembic（提示词推荐）**

- 首个 revision `0001_baseline` 用 `inspector.has_table()` 判断：库里已有 `sessions/tool_call_logs/audit_logs` 则**只 stamp 不建**，否则建表 ⇒ 无论新库老库都能收敛；
- `0002_auth_and_providers`：建 `users` / `llm_providers`、加 `user_id`/`provider_id` 列 + 索引；
- 启动时改为 `alembic upgrade head`（调 Python API，不用子进程），失败**拒绝启动**；
- 测试仍走 `create_all`（避免每个测试进程跑 alembic，拖慢且破坏现有 conftest 的 `os._exit` 收尾）。
- 代价：多一个依赖 + baseline 的"已有表"处理要小心（但 §7.2 里无论如何都要写这段幂等判断）。

**方案 B：幂等启动迁移脚本（`create_all` 风格）**

- `app/db/migrate.py`：查 `information_schema` → 缺表 `CREATE TABLE IF NOT EXISTS`、缺列 `ALTER TABLE ADD COLUMN`、缺索引 `CREATE INDEX`；
- 与现有 `main.py` 的 `Base.metadata.create_all()` 风格一致，零新依赖；
- 代价：每次加列都要手写"先查后改"，会持续积累技术债；无法回滚。

**我的倾向是 A**，因为本次涉及 5 张表的建/改 + 存量回填 + 后续还会演进（用户级挂载、审计等），手工幂等脚本的维护成本会持续上升。但 B 的"零依赖 + 风格一致"在单人自研项目里也完全合理。**请定案。**

### 7.3 存量数据迁移与回滚（两方案共用）

1. **备份**：脚本第一步 `mysqldump agent_harness > backend/data/backup_<ts>.sql`；工作区 `cp -r workspace workspace_backup_<ts>`；
2. **引导管理员**：从 `.env` 的 `BOOTSTRAP_ADMIN_EMAIL/PASSWORD` 创建；未配置则用固定默认值 + `logger.warning`（提示词 §3.1.4）；
3. **回填归属**：`sessions.user_id` ← 引导管理员；`tool_call_logs`/`audit_logs` 按 `session_id`/`thread_id` join 回填，孤儿留 NULL 并报数；
4. **工作区搬迁**（破坏性，单独一步）：
   `workspace/*` → `workspace/<admin_id>/`（含 `Agent.md`）。
   回滚：删新目录、`mv workspace_backup_<ts> workspace`（因是复制而非移动，**回滚无损**）。
   ⚠️ 执行前**必须停掉后端**（两个进程同时写 `data/harness.sqlite` 会让对方写锁卡死 —— 这个坑我们踩过）；
5. **系统供应商引导**：无系统级供应商时用 `.env` 的 DeepSeek 配置写一条（§5.1）；
6. **回滚**：`alembic downgrade -1`（方案 A）/ 手工 DROP（方案 B）+ 上面第 4 步反向。

---

## 8. 文件改动清单

### 8.1 后端 —— 新增

```
backend/app/core/security.py            JWT 签发/校验 + argon2 密码哈希
backend/app/core/crypto.py              Fernet 加解密 + 掩码
backend/app/core/workspace.py           workspace_root(user_id) 统一入口
backend/app/api/deps.py                 get_current_user / get_current_admin / get_owned_session
backend/app/api/routes/auth.py          注册 / 登录 / me
backend/app/api/routes/llm.py           供应商 CRUD + test + models
backend/app/agent/providers.py          供应商解析（会话→用户→系统）+ 参数容错表 + ProviderConfig
backend/app/db/migrate.py               （方案 B 时）幂等迁移
backend/alembic.ini + alembic/          （方案 A 时）迁移框架
backend/tests/test_p18_auth.py          认证流程
backend/tests/test_p18_isolation.py     跨用户隔离（含 SSE/运行/文件）
backend/tests/test_p18_model_factory.py 模型工厂（mock 网络）
backend/tests/test_p18_secrets.py       密钥加解密与脱敏
```

### 8.2 后端 —— 修改

| 文件 | 改动 |
|---|---|
| `app/core/config.py` | `JWT_SECRET`/`JWT_ALGORITHM`/`ACCESS_TOKEN_EXPIRE_MINUTES`/`FERNET_KEY`/`ALLOW_REGISTRATION`/`BOOTSTRAP_ADMIN_EMAIL`/`BOOTSTRAP_ADMIN_PASSWORD`/`CORS_ORIGINS`；`deepseek_*` 保留为引导配置并加注释说明新语义 |
| `app/db/models.py` | `User`、`LlmProvider` 新增；`Session` 加 `user_id`/`provider_id`；两个日志表加 `user_id` |
| `app/main.py` | CORS 收紧到 `settings.cors_origins`；`auth` router 单独挂载；启动迁移（替换裸 `create_all`）+ 引导管理员/供应商 |
| `app/api/routes/__init__.py` | `api_router` 加 router 级 `Depends(get_current_user)`；注册 `auth`、`llm` |
| `app/api/routes/{sessions,chat,chat_stream,approvals,hitl,files,skills}.py` | 归属校验（§4.3） |
| `app/agent/model.py` | **工厂重构**（§5.2） |
| `app/agent/context.py` | `build_summary_middleware(model)` |
| `app/agent/runtime.py` | `create_session(user_id)`；owner 解析；`_log_tool_event` 写 `user_id`；`get_agent` 用 owner 取工作区/供应商 |
| `app/agent/runner.py` | `SessionRun.user_id` + `start(..., user_id)` |
| `app/agent/permissions.py` | 作用域加 user 维度 |
| `app/agent/approvals.py` | 挂起登记带 user |
| `app/agent/prompt.py` | `memory_file_path(user_id)` |
| `app/agent/opensandbox_backend.py` | `user_workspace_root(user_id)`；按角色决定是否挂 `/mnt/*` |
| `app/schemas/chat.py` · `skill.py` | `ChatRequest` 加 `provider_id`；新增 auth/llm 的 schema 模块 |
| `backend/requirements.txt` | 补齐/新增（§7.1） |
| `backend/.env.example` | 新增全部配置项 + 生成命令说明 |
| `backend/tests/test_files_api.py`(17处) · `test_api_routes.py`(2) · `test_p4_skills.py`(1) · `test_p9_sse.py`(2) · `test_health.py`(1) | 共 **23 处** API 调用需带鉴权头（新增 `conftest.py` 的 `auth_client` fixture） |

### 8.3 前端

| 文件 | 改动 |
|---|---|
| `src/api.js` **新增** | 统一请求封装：注入 `Authorization`、401 → 清 token + 回登录页；`token/user` 的 localStorage 读写 |
| `src/AuthView.vue` **新增** | 登录 / 注册视图（中文错误提示） |
| `src/ProvidersView.vue` **新增** | 供应商列表（系统/我的标记、默认星标、启用开关、密钥掩码）+ 新增/编辑表单 + 测试连接 + 设为默认 + 删除 |
| `src/App.vue` | 顶层 `v-if="$user"` 门禁；`onMounted` 的 `loadSessions()` 移到登录成功之后；登出 + 用户信息；供应商设置入口；对话区模型选择器；空状态引导；**`LAST_SESSION_KEY` 按用户分区**（`lastSession:<user_id>`，否则切用户会去开别人的会话）；7 处散落 `fetch` 改走 `api.js` |
| `src/sse.js` | 6 处 `fetch` 走 `api.js`；`consumeStream` 收到 401 → 触发登出（SSE 流中也要处理） |
| `vite.config.js` | 无需改（代理已就绪） |

---

## 9. 分阶段实施（每阶段跑通 + 自测后再进入下一阶段）

| 阶段 | 内容 | 独立可验证点 |
|---|---|---|
| **0** | **本方案** | ← 你确认 §3 三处冲突 + §7.2 迁移方案 |
| **1** | 认证底座：`users` 表 + 迁移 + 引导管理员 + argon2/JWT + `auth` 路由 + `get_current_user` + 路由级默认拒绝 + CORS 收紧 | 未登录访问 `/api/v1/sessions` → 401；注册/登录/me 通；pytest 新用例通过；**已有 23 处测试调用补鉴权后全绿** |
| **2** | 全量隔离：所有路由归属校验 · `run_manager` 绑 user · 工作区/长期记忆按用户隔离 + **目录搬迁** · 存量数据回填 · `/mnt/*` 按角色 · HITL 作用域 | u1/u2 交叉访问全 404；u2 拿 u1 的 session_id 调 8 个端点全 404；文件/记忆互不可见；迁移备份与回滚演练 |
| **3** | 前端登录态：`api.js` + `AuthView` + 401 统一处理 + 登出 + 用户信息 | 端到端：登录 → 会话列表 → 发消息 → 刷新仍在；token 过期 → 自动回登录页 |
| **4** | 多供应商：`llm_providers` + 密钥加密 + **模型工厂重构（含摘要中间件 + DeepSeek reasoning 探针）** + 管理与测试 API + 系统供应商引导 | openai/anthropic 各配一条 test 通过；**日志证明主模型/摘要走所选供应商**；`reasoning_content` 仍可见 |
| **5** | 前端供应商管理 + 模型选择器 | 测试连接/设默认/删除/切换会话模型可用；未配置时空状态引导 |
| **6** | 回归与测试：补 §7.9 四类用例 + 全量回归 | SSE/审批/沙箱/文件/子智能体全流程；验收 §7 逐条演示 |

---

## 10. 验收标准 → 落实位置对照

| 验收 | 落实 |
|---|---|
| 1. 未登录 401 / 登录正常 | 阶段 1，router 级默认拒绝 + `test_p18_auth.py` |
| 2. u1/u2 交叉访问全 404 | 阶段 2，`get_owned_session` 统一入口 + `test_p18_isolation.py` |
| 3. u2 无法经 SSE/接回/文件路径读到 u1 数据或记忆 | 阶段 2，`SessionRun.user_id` 校验 + 工作区按用户物理隔离 + `Agent.md` 按用户 |
| 4. token 过期回登录页 | 阶段 3，`api.js` 401 拦截（含 SSE 流内） |
| 5. 双协议供应商，主/子/摘要都走所选模型 | 阶段 4，`test_p18_model_factory.py` + 真实调用日志；子智能体因"不传 model"天然继承 |
| 6. 密钥密文 / 接口不回明文 / 日志无密钥 | 阶段 4，`test_p18_secrets.py` + 日志 scrub |
| 7. 切换/删除/默认/失效提示清晰；旧 `.env` 可作系统供应商 | 阶段 4，供应商引导 + 中文错误归一 |
| 8. 原有能力回归 | 阶段 6，既有 `tests/test_p8_approvals.py`(24) 等 + 前端 `_verify/*.mjs` |
| 9. 四类 pytest | 阶段 1/2/4 分布落地 |
| 10. 迁移说明 + 从零启动步骤 | 阶段 6，`docs/P18-*` + `.env.example` + README |

---

## 11. 我主动提出的三条优化（相对提示词）

1. **密码哈希用 argon2-cffi 替代 bcrypt**：已装（零新依赖）、无版本兼容坑。→ 唯一可能的新增依赖只剩 alembic。
2. **`ChatDeepSeek` 保留为 openai 协议下的 DeepSeek 特化分支**：避免换 `ChatOpenAI` 后丢失 `reasoning_content`（"思考增量"会黑屏，这是 P14 专门解决的问题）。并把"真跑一次验证 reasoning 仍在"列为阶段 4 第一个探针。
3. **明确"`AsyncSqliteStore` 不做 namespace 改造"**：实测 store 表只有 1 行 P3 期遗留数据、当前记忆走文件通道，改造无收益；改为加只读回归探针。（提示词 §3.3.6 把 store 列为"重点容易漏"，实测结论是**这个项目不适用**——如实说明，不做过场戏。）

---

## 12. 定案记录（2026-09-18）

| 项 | 决定 | 影响 |
|---|---|---|
| 工作区 / 长期记忆隔离粒度 | **按用户** `workspace/<user_id>/` | §3 冲突 1 采纳方案 A |
| P12 本机磁盘 `/mnt/*` | **保持全局开放**（显式接受风险） | §3 冲突 2；隔离验收范围限定为「Harness 内数据」，`/mnt` 排除在外 |
| 数据库迁移 | **Alembic** | §7.2 方案 A；新增依赖 alembic；启动改 `alembic upgrade head`，测试仍用 `create_all` |
| 密码哈希 | **argon2-cffi**（已装） | §7.1；零新依赖 |

**尚待你确认的一项**（未回答，我按建议执行，你可随时推翻）：

- **HITL 开关作用域**：全局切换降为 admin 专属 + 挂起中断清理按 user 过滤（§3 冲突 3）。

**由此确定的依赖新增清单**：仅 `alembic` 一项（`PyJWT` / `cryptography` / `langchain-anthropic` /
`argon2-cffi` 均已在 `agent_env` 中就绪）。`requirements.txt` 另做一次补齐（把实际在用但未声明的
`pymysql` / `aiosqlite` / `pyyaml` / `langchain-tavily` / `opensandbox` 写进去，移除未使用的 `psycopg2-binary`）。

---

## 13. 施工顺序（阶段 1 的落脚点）

等你说"开始阶段 1"，我会按这个顺序动手，每步独立可验证：

1. `config.py` 新增配置项 + `.env.example` 同步 + `.env` 补 `JWT_SECRET`/`FERNET_KEY`/`BOOTSTRAP_ADMIN_*`（**密钥我会生成好写进 `.env`，不打印到对话里**）；
2. `alembic init` + `0001_baseline`（`has_table()` 兼容老库）+ `0002_auth`（建 `users`、加 `user_id`/`provider_id` 列与索引）；
3. `app/core/security.py`（argon2 + JWT）、`app/api/deps.py`；
4. `app/api/routes/auth.py` + 在 `main.py` 单独挂载（不经过受保护的 `api_router`）；
5. `api/routes/__init__.py` 加 router 级 `Depends(get_current_user)`（默认拒绝）；
6. `main.py` CORS 收紧到 `settings.cors_origins`；
7. 迁移运行时建引导管理员 + 存量回填；
8. `tests/conftest.py` 加 `auth_client` fixture，修好现有 23 处调用；
9. `tests/test_p18_auth.py` 跑绿。

⚠️ 两个执行注意（都是这个项目踩过的坑）：
- 迁移和验证期间**不要起第二个后端进程**去动同一个 `data/harness.sqlite`（会让对方写锁卡死）；
- 迁移前先 `mysqldump` + 备份 `workspace/`；**目录搬迁那一步（阶段 2）必须等你停掉 8000 端口的后端**；
- 你常驻的 8000 + 5173 跑的是旧代码，**改完后端我会提醒你重启**。

