# P13 设计：动态载入 Skills / Subagents / MCP 的 Agent 构建层

> 状态：**设计稿（未实施）**
> 前置阅读：[P4-skills-design.md](P4-skills-design.md)、[P6-subagents-design.md](P6-subagents-design.md)、[P7-mcp-design.md](P7-mcp-design.md)、[P8-hitl-security-design.md](P8-hitl-security-design.md)
> 实测数据：[`_probe/agent_build_cost_probe.py`](../_probe/agent_build_cost_probe.py)、[`agent_build_breakdown_probe.py`](../_probe/agent_build_breakdown_probe.py)、[`agent_build_attribution_probe.py`](../_probe/agent_build_attribution_probe.py)

---

## 0. 结论摘要

| 问题 | 结论 |
|------|------|
| 每次对话都建 agent，开销大吗？ | **实测重建 233ms，其中 95% 是框架组装，IO 只占 2%**（见 §1）。所以"优化 IO"是错误方向。 |
| 需要每次对话都建吗？ | **不需要，而且现在也没这么做** —— `_agents` 按会话缓存（`runtime.py:42`），命中后 0ms。 |
| 真正的成本在哪？ | **不在"建了几次"，而在"缓存键选错了"** —— 现在按 `session_key` 缓存，而 session 是唯一不该进缓存键的东西（见 §2）。 |
| 有更合适的方法吗？ | 有。把缓存键从 **session** 换成 **配置指纹**，graph 跨会话共享；per-session 的部分下沉到运行时（见 §3、§5）。 |

**核心设计一句话**：`get_agent()` 里现在混了三种生命周期完全不同的东西（进程级配置 / 配置派生 / 会话绑定），把它们分开之后，"每会话建 agent"这个问题会自动消失。

---

## 1. 实测：233ms 花在哪

在 `HARNESS_MACHINE_ACCESS=true`、`SANDBOX_ENABLED=true`、`MCP_ENABLED=false`、3 个本机挂载点、1 个 subagent 的配置下：

```
get_agent() 冷启动（首次）                    320.22 ms
get_agent() 命中 _agents 缓存                   0.00 ms
get_agent() 强制重建（invalidate 后）          233.34 ms
```

**逐项归因**：

| 项 | 耗时 | 占比 | 说明 |
|----|------|------|------|
| `build_llm()` × **2** | 70.48 ms | 30% | `runtime.get_agent` 一次 + `context.build_summary_middleware` 一次，两个独立 httpx client |
| `create_deep_agent` 组装 | 61.80 ms | 26% | 框架内部成本 |
| 未归因残差 | 72.03 ms | 31% | 首轮 import、`memory=` / `permissions=` 中间件、tavily 工具 schema 编译 |
| `machine_mounts()` × **6** | 8.22 ms | 4% | 重复计算，可缓存 |
| skills 编译 + 播种 + YAML 读 | 5.43 ms | 2% | **原本以为的大头，实测很小** |
| `build_failure_limit_middlewares()` | 5.29 ms | 2% | |
| `build_main_backend` | 4.08 ms | 2% | |
| subagents YAML 读 × 2 | 3.36 ms | 1% | `validate_permissions()` 与 `build` 各读一次 |
| `validate_permissions` | 2.65 ms | 1% | |

**框架地板价对照**（同一个 LLM 实例，纯 `create_deep_agent`）：

```
最小：仅 model + prompt + backend          30.07 ms
＋ 中间件（7 个）                          43.17 ms
＋ 子智能体（1 个，含独立 FilesystemMiddleware） 61.80 ms
```

**由此得到三条判断**：

1. **IO 不是瓶颈**（7.11ms / 3%）。当前"每次构建都重读 YAML、重写 SKILL.md、重播种虚拟 FS"看起来很浪费，但优化它只有个位数毫秒收益——**优先级最低**。
2. **`build_llm()` 被调了 2 次是个真 bug**：两个 `ChatDeepSeek` = 两个 httpx 连接池，70ms，占总重建 30%。这是最容易摘的果子。
3. **框架地板价 30ms，完整配置 62ms**。也就是说 `create_deep_agent` 本身很便宜——**"重建"这件事没那么可怕，可怕的是重建的触发条件和缓存键**。

---

## 2. 真正的问题：缓存键选错了

当前实现：

```python
# runtime.py:42
_agents: dict[str, tuple[object, object]] = {}      # 键 = session_key

def invalidate_agents(session_key=None):
    if session_key:
        dropped = [session_key] if _agents.pop(session_key, None) is not None else []
    else:
        dropped = list(_agents.keys())
        _agents.clear()                              # ← 全局清空
```

### 2.1 为什么现在"必须"按会话建 —— 一条因果链

```
沙箱按会话隔离（SandboxRegistry 的原则：不同会话必须隔离）
  → OpenSandboxBackend 在 __init__ 里固定 session_key（opensandbox_backend.py:343-345）
  → build_main_backend(session_key=...) 产出会话专属 backend
  → backend 是 create_deep_agent 的**构建期参数**
  → permissions 也可能按会话覆盖（agent_permissions(session_key)）
  ⇒ graph 只能按会话建
```

**这条链里唯一真正"会话级"的东西是沙箱容器绑定。** 其余（skills / subagents / MCP / prompt / model / 中间件）全是进程级的。

### 2.2 两个具体症状

**症状 A：`invalidate_agents()` 的代价与收益严重不成比例。**

HITL 开关切换（`hitl.py:44`）会调 `invalidate_agents(None)` → **清空所有会话的 agent**。下一个请求开始，每个会话都要各付 233ms。而实际变化只是一个布尔值——可以把它编进指纹，让"开关切过去又切回来"命中旧条目（见 §3.2）。

**症状 B：`_agents` 无界增长。**

键是 session_key，值是 `(agent, backend)`，且**没有任何淘汰**。删会话（`sessions.py:delete_session`）也不清理（见审查报告 R4）。会话多了以后，N 个 graph + N 个 backend + N 个沙箱引用常驻。

### 2.3 关键认知：LangGraph 的 compiled graph 是无状态的

`agent.astream(input, config)` 的所有会话状态都在 `checkpointer` 里，按 `config["configurable"]["thread_id"]` 区分。**同一个 compiled graph 可以被任意多个 thread 并发使用。**

所以"一个 graph 只能服务一个会话"从来不是框架的限制，是本项目把 backend 绑进构建期导致的。

---

## 3. 设计

### 3.1 三层拆分

```
┌─ Layer 1：资源层（进程级长驻，与 graph 完全无关）────────────────┐
│  SpecRegistry                                                  │
│   ├── skills      编译产物 + 内存快照（读一次，版本变化才重读）  │
│   ├── subagents   SubAgentConfig 列表（Pydantic 对象，非 SubAgent）│
│   ├── mcp         MCPConnectionPool（长连接池，按 server 名缓存）│
│   └── versions    {skills: n, subagents: n, mcp: n, prompt: n}  │
└────────────────────────────────────────────────────────────────┘
                              │ 只有版本变化才重算
                              ▼
┌─ Layer 2：编译层（按配置指纹缓存 graph，跨会话共享）───────────┐
│  fingerprint = sha256(skills_v, subagents_v, mcp_v,            │
│                       hitl_enabled, prompt_v, model_name)      │
│  _compiled: dict[fingerprint, CompiledGraph]   ← 带 LRU 上限   │
└────────────────────────────────────────────────────────────────┘
                              │
                              ▼
┌─ Layer 3：会话层（每请求，接近零成本）────────────────────────┐
│  config = {"configurable": {"thread_id": session_id}}          │
│  backend = 按会话解析（沙箱绑定）                               │
└────────────────────────────────────────────────────────────────┘
```

### 3.2 失效策略：版本号 + 按需失效

**"动态载入" ≠ "每次都重建"。** 正确做法是记录版本、只在真变了时失效：

```python
class SpecRegistry:
    """进程级配置注册表：缓存"解析后的配置"，并按版本号对外暴露失效信号。"""

    def __init__(self) -> None:
        self._versions = {"skills": 0, "subagents": 0, "mcp": 0, "prompt": 0}

    def bump(self, kind: str) -> None:
        """显式宣告某类配置变了（由 API / watcher / reload 端点调用）。"""
        self._versions[kind] += 1

    def fingerprint(self, *, hitl_enabled: bool) -> str:
        payload = json.dumps(
            {**self._versions, "hitl": hitl_enabled, "model": settings.deepseek_model},
            sort_keys=True,
        )
        return hashlib.sha256(payload.encode()).hexdigest()[:16]
```

**三种触发方式**（按需组合，不必全上）：

| 方式 | 实现 | 适用 |
|------|------|------|
| 显式 API | `POST /api/v1/skills` 成功后 `registry.bump("skills")` | 已有写入口，最可靠 |
| mtime 轮询 | 每次请求前 stat `configs/**` 的 mtime，对不上就 bump | YAML 被手工编辑的场景，成本极低 |
| reload 端点 | `POST /api/v1/admin/reload` | 兜底与调试 |

**收益对照**（HITL 开关切换）：

| | 现在 | 改后 |
|---|---|---|
| 动作 | `_agents.clear()` 清空全部 | 只是 fingerprint 多一个条目 |
| 代价 | 所有会话下次各付 233ms | 首次各付一次；**切回来命中旧条目，0ms** |

### 3.3 `get_agent()` 改写形态

```python
async def get_agent(session_key: str = _MAIN_SESSION):
    session_key = session_key or _MAIN_SESSION
    fp = spec_registry.fingerprint(hitl_enabled=permissions.hitl_enabled(session_key))

    graph = _compiled.get(fp)
    if graph is not None:
        return graph

    # 只有这里才做"昂贵"的事，而且每个指纹只做一次（跨会话共享）
    graph = await _compile(fingerprint=fp)
    _compiled.put(fp, graph)          # 带 LRU 上限
    return graph
```

`_compile()` 的输入全部来自 `SpecRegistry` 的缓存，**不再有任何每会话 IO**。

### 3.4 per-session 的沙箱怎么办 —— 两个方案

这是整个设计里唯一需要产品决策的地方。

#### 方案 A：保留 per-session 容器，但把 session 从构建期移到运行时

让 backend 不再在构造时固定 `session_key`，而是每次方法调用时从运行时上下文取。

- **可行性需要先验证**：`runtime.execution_info.thread_id` 在 `FilesystemMiddleware` 的工具调用路径上是否可拿。参考熔断中间件 `_thread_id()`（`middleware.py:237-254`）——它确实能从 `ToolCallRequest.runtime.execution_info` 拿到 thread_id，**所以路径是存在的**。但 backend 的方法签名里没有 runtime 参数，需要确认 deepagents 是否在某处把 runtime 注入进 backend 调用。**这是一个必须先验证的假设，不能直接开工。**
- 收益：graph 可全局共享，同时保留会话隔离。

#### 方案 B：沙箱池化，"借—用—还"（推荐）

容器不再跟会话绑定，而是执行时从池里借一个、用完归还。backend 只持有一个池引用 → 进程级单例。

- **与项目既有定位一致**：README 与 `docs/P11` 反复强调"**容器只是一次性算力**"、"文件系统只有一份真相（本地磁盘）"。既然容器本来就无状态，per-session 容器是**不必要地强**的隔离。
- 代价：同一容器会被不同会话复用。若需多租户隔离，则**不能选 B**。
- 附带收益：容器数量从 O(会话数) 降到 O(池容量)，`SandboxRegistry.get()` 里那个"锁内做 300s 建容器 IO"的问题（审查报告 O9）也随之缓解。

**建议**：单用户/内部工具选 B；面向多租户选 A。

### 3.5 把重建本身做便宜（与选型无关，都该做）

| 优化 | 收益 | 风险 |
|------|------|------|
| `build_llm()` 加 `@lru_cache` | **-35ms** | 低。`ChatDeepSeek` 无状态，httpx pool 支持并发 |
| `machine_mounts()` 加 `@lru_cache`（或 `SpecRegistry` 缓存） | -7ms，且消除 6 次重复计算 | 低。挂载点在进程生命周期内不变 |
| `validate_permissions()` 只在 `subagents` 版本变化时做 | -3ms | 低。它本质是"配置校验"，不是"每会话校验" |
| `compile_all_skills()` / `seed_skills()` 只在 `skills` 版本变化时做 | -5ms，**且不再每次构建都写盘** | 低 |
| 归因残差（首轮 import / memory / permissions / tool schema） | ? | 需进一步测量，预计可再省 30-50ms |

合计：**233ms → 预计 ~120ms**（不含未归因部分的进一步优化）。这一阶段**完全不改架构**，可以先独立做掉。

### 3.6 MCP：真正的难点，也是必须提前设计的地方

MCP 与前两者有本质区别 —— 它是 **session-based 协议**，工具必须依附一个活着的连接。

#### 当前实现有两个需要验证/修正的点

```python
# mcp.py:83-91
async def _build_tools_one(cfg: McpServerConfig) -> list:
    from langchain_mcp_adapters.tools import load_mcp_tools
    from mcp.client.streamable_http import StreamableHttpTransport

    async with StreamableHttpTransport(url=cfg.url, headers=cfg.headers or None) as transport:
        tools = await load_mcp_tools(transport=transport, client_name=cfg.name)
    return tools          # ← async with 已退出，transport 已关闭
```

1. **生命周期**：`async with` 块退出后 transport 就关了，而返回的 tools 是否仍然可用，取决于适配器实现。按 MCP 协议的正常用法，工具必须在 session 存活期间调用 —— **这个写法很可能返回一组"死"工具**。
2. **API 名称**：`StreamableHttpTransport` / `load_mcp_tools(transport=...)` 与主流的 `streamablehttp_client` + `ClientSession` + `load_mcp_tools(session)` 写法不同。

加上 `langchain_mcp_adapters` **当前未安装**（实测 `MISSING`），说明**这段代码从未真正执行过**。启用 MCP 前必须先做一次连通性验证，不能假设它能工作。

#### 正确形态：连接池在 graph 之外

```
MCPConnectionPool（进程级）
├── sessions: dict[server_name, ClientSession]     # 长驻，带 health check + 自动重连
└── tools:    dict[server_name, list[BaseTool]]    # 工具对象只持有 session 引用
```

关键点：**graph 重建不能导致重连**。工具对象从池里取，池的生命周期独立于 graph。否则每次 `invalidate_agents()` 都会引发一次 MCP 重连风暴——这正是"把连接建在 factory 里"的典型后果。

#### 进阶：让 MCP 工具完全免重建（可选）

如果希望 MCP 热插拔**完全不触发 graph 重建**，可以不把每个 MCP tool 注册成独立工具，而是注册两个固定工具：

- `list_mcp_tools()` → 列出当前可用的 MCP 工具及其参数 schema
- `mcp_call(name, arguments)` → 运行时派发

这样 graph 的 tool schema 恒定，增删 MCP server 完全不动 graph。代价是模型多一跳（可靠 prompt 引导 + 结果缓存缓解），且失去 schema 级参数校验。

| | eager registration（每工具一个） | lazy dispatch（代理两个） |
|---|---|---|
| graph 重建 | 需要 | **不需要** |
| 参数校验 | 框架自动 | 需自己校验 |
| 模型跳数 | 1 | 2（可优化） |
| 适用 | MCP 配置稳定 | MCP 频繁增删 |

---

## 4. 迁移路径

### Phase 1 —— 只降成本，不改架构（低风险，可独立上线）

1. `build_llm()` 加 `@lru_cache`
2. `machine_mounts()` 缓存
3. `validate_permissions()` / `compile_all_skills()` / `seed_skills()` 改为按版本号执行
4. `_agents` 加 LRU 上限（先解决无界增长）
5. 补删会话时的清理：同时调 `registry.destroy(session_id)` + `invalidate_agents(session_id)`（审查报告 R4）

**产出**：重建 233ms → ~120ms；行为与对外接口完全不变。

### Phase 2 —— 缓存键换成指纹（需要 Phase 1 前置）

6. 新增 `SpecRegistry`（版本号 + fingerprint）
7. `_agents[session]` → `_compiled[fingerprint]`，带 LRU
8. HITL 切换改为改指纹，不再全局清空
9. **前置必做**：修中间件实例状态问题 —— `LoggingMiddleware` / `AuditMiddleware` 把计数存在 `self`（`middleware.py:560/601`）。graph 一旦跨会话共享，这些计数会跨会话串（审查报告 O4）。修法：计数移进 graph state（带 reducer 的 key），或按 `thread_id` 分桶存 registry（照抄熔断的做法）。

**产出**：graph 跨会话共享；配置变化只多一个缓存条目。

### Phase 3 —— 解绑 session 与 backend（需产品决策）

10. 选 §3.4 的方案 A 或 B
11. 若选 A，先做**可行性验证**：确认 backend 方法调用路径上能拿到 `runtime.execution_info.thread_id`
12. 若选 B，重构 `SandboxRegistry` 为池化借还

**触发条件**：会话创建频繁（如 >50/天）或常驻内存吃紧。否则**不必做** —— Phase 1+2 已经解决 90% 的问题。

### Phase 4 —— MCP（独立推进）

13. 先做连通性验证（`langchain_mcp_adapters` 安装 + transport/生命周期写法确认）
14. 实现 `MCPConnectionPool`，池的生命周期独立于 graph
15. 按需决定是否上 lazy dispatch

---

## 5. 风险与注意事项

| 风险 | 说明 |
|------|------|
| **中间件实例状态跨会话串** | Phase 2 的硬前置。不修就是引入一个新 bug 换掉一个性能问题。 |
| **`_compiled` 无界增长** | 指纹是哈希，正常情况下种类很少（配置组合数），但 HITL 是布尔 + 配置版本递增 → 版本号单调递增会让指纹**永不重复**。建议：HITL 不进版本号计次，只用当前值；版本号用"内容指纹"而非累加计数。 |
| **共享 graph 的并发安全** | 单个 compiled graph 支持并发 thread，但要复核所有中间件是否都是无状态的（不止 Logging/Audit，还有 `TodoListMiddleware`、`SkillsMiddleware`、`SummarizationMiddleware`）。 |
| **沙箱复用（方案 B）的隔离性** | 若将来要面向多租户，B 方案必须回退。建议现在就把"容器隔离级别"写成显式配置项，而不是隐含在 `SandboxRegistry` 的键里。 |
| **MCP 未验证** | 在 Phase 4 的连通性验证通过前，不要把 MCP 写成"已支持"。当前 `MCP_ENABLED=false` 且适配器未安装，等于该功能不存在。 |

---

## 6. 附：本次测量方法

三个探针脚本（保留在 `_probe/`，可复跑）：

| 脚本 | 作用 |
|------|------|
| `agent_build_cost_probe.py` | 拆阶段：纯配置 IO / 会话级 IO / `get_agent` 冷启动 vs 缓存 vs 重建 |
| `agent_build_breakdown_probe.py` | 数 `build_llm()` 调用次数；测 `create_deep_agent` 的地板价与逐层增量 |
| `agent_build_attribution_probe.py` | 归因残差：`machine_mounts()` 调用次数与累计耗时；总账对账 |

> **运行注意**：这三个脚本必须在结束时 `os._exit(0)`。`context.py` 的 aiosqlite 全局连接不释放会让解释器退出挂起（同 `backend/conftest.py` 的说明）。直接跑会看起来像"命令卡死"。
