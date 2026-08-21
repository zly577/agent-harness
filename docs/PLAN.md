# Agent Harness 项目计划书（v0.1）

> 状态：已确认 · 基线文档 · 随实施持续更新

## 0. 项目定位

基于 **LangChain + LangGraph 的 DeepAgents 框架**，从零搭建一套**自有 Harness 架构的通用智能体系统**。

- 本阶段范围：Agent 核心 + Harness 服务层（FastAPI 作为运行时编排与验证入口）
- 后续范围（暂不设计）：用户登录、多租户、前端深化（Vue3）
- 核心约束：八大能力全部基于 DeepAgents 框架能力实现，不重复造轮子；每一步代码先经人工审查

## 1. 技术栈（已确认）

| 层 | 选型 |
|----|------|
| Agent 框架 | deepagents（底层 = LangChain + LangGraph） |
| 后端 | FastAPI |
| 数据库 | PostgreSQL（本地开发用 Docker Compose） |
| 前端（后期） | Vue3 |
| 模型 | DeepSeek（deepseek-v4-flash，OpenAI 兼容接口，base_url 可配） |
| Python 环境 | 3.12（conda 环境 `agent_env`，已满足 deepagents 的 ≥3.11 要求，无需升级） |

## 2. 总体架构（四层）

```
┌─────────────────────────────────────────────────────────┐
│  前端层 Vue3（后期）  对话界面 / 人工介入审批台 / 运行监控    │
├─────────────────────────────────────────────────────────┤
│  Harness 服务层 FastAPI                                  │
│  会话管理 · 运行时编排 · 审批流(HITL) · 安全拦截            │
│  Skills注册表 · 子智能体注册表 · MCP配置管理 · 审计日志      │
├─────────────────────────────────────────────────────────┤
│  Agent 核心层 DeepAgents(LangGraph)                      │
│  Agent / Task / Guardrail / Handoff / 中间件             │
│  规划器 · 上下文管理 · 虚拟FS沙箱 · Skills工具 · MCP工具    │
├─────────────────────────────────────────────────────────┤
│  数据层 PostgreSQL：会话/消息/审批记录/配置/审计            │
└─────────────────────────────────────────────────────────┘
```

设计原则：**Agent 核心层只依赖 DeepAgents 的机制**；Harness 服务层负责把"配置（YAML/DB）→ 运行时"组装起来，业务逻辑不侵入核心层。

## 3. 八大能力 → DeepAgents 组件映射

| # | 要求 | DeepAgents 对应机制 | 落地设计 |
|---|------|--------------------|---------|
| 1 | 规划能力 | `RunnerConfig.planner`（规划器 Agent） | 规划器产出结构化任务清单，执行器按计划逐步执行，计划变更需显式记录 |
| 2 | 上下文管理 | LangGraph state + message 管理 | 会话状态持久化到 PostgreSQL；超限用 trimmer + 摘要压缩；跨轮次恢复 |
| 3 | 虚拟文件系统沙箱 | 工具集（官方示例用 E2B，我们自建） | 阶段一：进程内虚拟文件系统（严格路径边界、白名单操作、可单测）；可选接 Docker/E2B |
| 4 | 中间件 | deepagents/LangGraph middleware 机制 | 统一中间件管道：日志、审计、限流、上下文注入、安全扫描（API 以实际安装版本为准） |
| 5 | Skills（YAML 配置 + Agent 可创建） | LangChain 动态工具 | `skills/*.yaml` → Pydantic 校验 → 动态构建 Tool；`create_skill` 工具让 Agent 生成 YAML，写入前走人工审批 |
| 6 | 多智能体/子智能体委派 + 隔离 | `Task`（子任务委派）+ `Handoff`（Agent 交接） | `subagents/*.yaml` 声明子智能体 → 注册表动态加载；隔离 = 独立 state + 工具权限矩阵 |
| 7 | MCP 接入（魔塔社区） | langchain-mcp-adapters（含 MCPModelScopeStdio） | `mcp/*.yaml` 配置 MCP server，统一抽象成 Tool 适配层 |
| 8 | 人工介入 + 安全边界 | LangGraph `interrupt()` + Guardrail（输入/输出/步骤三层） | 审批点模型：敏感操作触发 interrupt，FastAPI 提供审批/拒绝接口；Guardrail 三层校验 |

## 4. 目录结构

```
agent-harness/
├── backend/
│   ├── app/
│   │   ├── main.py                 # FastAPI 入口
│   │   ├── api/                    # 会话、审批、配置管理路由
│   │   ├── core/                   # 全局配置、安全
│   │   ├── db/                     # SQLAlchemy + PostgreSQL 模型/会话
│   │   ├── agent/                  # ★ Agent 核心层（P1 起填充）
│   │   │   ├── runtime.py          #   图编译与运行
│   │   │   ├── planner.py          #   规划器
│   │   │   ├── context.py          #   上下文管理
│   │   │   ├── middlewares/        #   中间件
│   │   │   ├── sandbox/            #   虚拟文件系统
│   │   │   ├── skills/             #   YAML→Tool 注册表
│   │   │   ├── subagents/          #   子智能体注册表
│   │   │   ├── mcp/                #   MCP 适配
│   │   │   └── guardrails/         #   三层安全校验
│   │   └── schemas/                # Pydantic 模型
│   ├── configs/
│   │   ├── skills/*.yaml
│   │   ├── subagents/*.yaml
│   │   └── mcp/*.yaml
│   └── tests/
├── frontend/                       # Vue3（后期）
├── docs/
└── docker-compose.yml              # 本地 PostgreSQL
```

## 5. 分阶段路线（每阶段有审查点）

| 阶段 | 内容 | 交付物 | 验证方式 |
|------|------|--------|---------|
| P0 ✅ | 项目骨架：目录、依赖、配置、FastAPI 空服务 | 可启动的空服务 | curl /health |
| P1 ✅ | 最小可用 Agent：DeepAgents Agent + 对话 API + 内存上下文 | 能对话的最小闭环 | API 发消息收回复 |
| P2 ✅ | 上下文管理：消息历史 SQLite 持久化（langgraph checkpoint/store）+ MySQL 业务库 + 上下文压缩 | 跨轮次&跨进程记忆 | 长对话测试 |
| P3 ✅ | 虚拟文件系统沙箱（StoreBackend + InMemoryStore，判断路由：默认可写不可执行） | 文件工具集 + execute 禁用 | 工具调用测试 |
| P4 ✅ | Skills：YAML 定义编译 → skill 播种 → SkillsMiddleware 复用 + create_skill 草稿 API | Skills 注册表 | 新增/调用 Skill |
| P5 ✅ | 中间件框架（Audit 入库 MySQL + Logging + 限流护栏） | 日志/审计管道 | 运行观测 |
| P6 ✅ | 多智能体：子智能体 YAML 注册表 + Task 委派 + 隔离（同一模型） | 多智能体协作 | 委派场景测试 |
| P7 ✅ | MCP 接入（可插拔 YAML 注册 + 适配层 + 安全降级） | MCP 工具适配 | 框架测试（真实 MCP 待网恢复） |
| P8 ✅ | 人工介入 + 安全边界：三层 Guardrail + interrupt 审批流 + 审批 API | 完整 HITL + 拦截 | 敏感操作审批测试 |
| P9 ✅ | Vue3 前端：对话（SSE 流式）+ 审批台 | 可用 Web 界面 | 后端 SSE 验证；前端在普通终端运行 |
| P10 | 测试补全、文档、部署配置 | 可上线版本 | 部署演练 |

## 6. 部署方案建议（P10 再定）

| 方案 | 适合场景 | 结论 |
|------|---------|------|
| ① Docker Compose 单机 | 开发/个人/小团队 | **开发阶段用此** |
| ② 单台云服务器 + Docker Compose + Caddy HTTPS | 正式上线初期 | **上线用此**（约 ¥100/月量级） |
| ③ 容器服务 ACK/K8s | 规模变大后 | 届时再评估 |
| ④ Serverless | 不推荐（长连接+流式受限） | 排除 |

## 7. 优化建议记录

### 设计期建议
1. 规划与执行分离：规划器只产计划，执行按步骤推进，计划变更显式记录
2. 沙箱先内后外：进程内虚拟 FS 先闭环，Docker/E2B 作可选后端（策略模式预留）
3. Skills 双重校验：Pydantic schema 校验 + Agent 创建的 Skill 默认草稿态，人工审批后激活
4. 子智能体隔离用权限矩阵：执行层强制，不靠提示词
5. MCP 统一适配层：所有 MCP 统一成同一种 Tool 接口
6. 安全边界三层化：输入 Guardrail → 步骤 Guardrail → 输出 Guardrail
7. 观测性优先：中间件从 P5 起日志/审计入库
8. 配置全部外置：密钥/地址一律走 .env / 环境变量

### 实施期新增（按阶段追加）
- P0-1：DB 迁移（alembic）推迟到 P2 与真实模型同步生成，避免空迁移脚手架
- P0-2：使用现有 conda 环境 `agent_env`（Python 3.12.13），deepagents 0.7.1 / langchain 1.3.14 / langchain-core 1.5.3 / langgraph 1.2.6 生态已预装且满足要求，仅补 fastapi/pytest/psycopg2-binary，共享环境零改动；依赖安装后生成锁文件固定版本
- P0-3：API Key 只进 `.env`（gitignored），不进代码/文档
- P0-4：沙箱 workspace-write 模式下 agent_env（工作区外）不可写，依赖安装需提升权限（danger-full-access）+ 审批

## 8. 链路图（终版完成后更新）

```
用户请求 → FastAPI(会话API) → 加载会话上下文(PostgreSQL)
  → 组装运行时(主Agent + 子智能体 + Skills + MCP工具 + 中间件 + Guardrail)
  → LangGraph 执行：
      中间件管道 → [规划器产计划]
      → 每步：输入Guardrail → Agent决策 → 工具调用
            ├─ 虚拟FS / Skills / MCP / 子智能体委派
            └─ 敏感操作 → interrupt() 挂起 → 人工审批台 → resume/拒绝
      → 输出Guardrail → 结果
  → 持久化(消息/审批记录/审计) → 流式返回前端
```

## 9. 决策记录

| 阶段 | 决策 | 说明 |
|------|------|------|
| P0 | 模型 deepseek-v4-flash，Key 存 .env | 模型名待用户与 provider 确认（P1 实测） |
| P0 | 使用 conda 环境 agent_env（Python 3.12.13） | 已满足 deepagents ≥3.11，无需升级 Python；deepagents 生态已预装，共享环境零改动 |
| P2 | 消息历史改用 **SQLite** checkpoint/store，业务数据改用 **MySQL** | PostgreSQL 因本机无 PG + HTTPS 出网被断 + 不启动 Docker 而不可行；MySQL 8.0 本机运行可用（root 账号），langgraph 仅原生支持 checkpoint/store 的 PG/SQLite/内存 |
| P2 | 上下文压缩用 langgraph 官方 `SummarizationMiddleware` | 不手写 SQLite 读 state，借用 deepagents/langchain 正规中间件 |
| P2 | alembic 推迟到 P4 前 | 当前用 `Base.metadata.create_all` 轻量建表；模型多后换正式迁移 |
