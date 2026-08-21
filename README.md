# Agent Harness

基于 **DeepAgents（LangChain + LangGraph）** 的自有 Harness 架构通用智能体系统。

当前进度：**P2 上下文管理**（消息历史 SQLite 持久化 + MySQL 业务库 + 上下文压缩）。

## 技术栈

- Agent 框架：deepagents（LangChain + LangGraph）
- 后端：FastAPI + Pydantic v2
- 消息历史/短期记忆：SQLite（langgraph `AsyncSqliteSaver`/`AsyncSqliteStore`）
- 业务数据：MySQL 8.0（会话、后续 Skills/子智能体/审计）
- 模型：DeepSeek（deepseek-v4-flash，OpenAI 兼容接口）
- 运行环境：conda `agent_env`（Python 3.12.13，deepagents 生态已预装）
- 前端：Vue3（P9 起）

## 目录

```
agent-harness/
├── backend/
│   ├── app/            # FastAPI 应用
│   │   ├── main.py     # 入口 + /health
│   │   ├── core/       # 全局配置（.env 驱动）
│   │   ├── db/         # SQLAlchemy（MySQL 业务库）
│   │   ├── agent/      # Agent 核心层（runtime/context/model）
│   │   ├── api/        # 路由（会话）
│   │   └── schemas/    # Pydantic 模型
│   ├── configs/        # skills / subagents / mcp 的 YAML 配置
│   └── tests/
├── docs/               # PLAN.md / P2-storage-design.md
└── docker-compose.yml  # （可选）未来若恢复 Docker 可托管 PG
```

## 快速开始

```powershell
# 1. 安装依赖（agent_env，Python 3.12.13）
cd D:\Deepseek_Harness\agent-harness\backend
C:\Users\Administrator\anaconda3\envs\agent_env\python.exe -m pip install -r requirements-dev.txt

# 2. 配置环境变量（.env 已就绪；确认 MYSQL_URL 与你的 MySQL 一致）
#    所需：MySQL 8.0 运行中，agent_harness 库存在，root 可连

# 3. 启动（自动建 MySQL 表 + SQLite checkpoint）
C:\Users\Administrator\anaconda3\envs\agent_env\python.exe -m uvicorn app.main:app --reload

# 4. 对话与健康检查
Invoke-RestMethod http://127.0.0.1:8000/health
Invoke-RestMethod -Uri http://127.0.0.1:8000/api/v1/chat -Method Post `
  -Body (@{ message = "你好" } | ConvertTo-Json) -ContentType "application/json; charset=utf-8"
```

> 存储说明：**SQLite**（`backend/data/harness.sqlite`）存消息历史/短期记忆（langgraph checkpoint/store，跨进程重启不丢）；**MySQL**（`agent_harness.sessions`）存业务会话元信息。详见 [docs/P2-storage-design.md](docs/P2-storage-design.md)。

## 安全约定

- 所有密钥只放 `.env`（已被 .gitignore 排除），绝不进代码/文档。
- 子智能体、Skill、MCP 的 YAML 配置一律经 Pydantic 校验后加载。

## 路线图

P0 骨架 ✅ → P1 最小 Agent ✅ → P2 上下文持久化 ✅ → P3 虚拟FS沙箱 ✅ → P4 Skills ✅ → P5 中间件 ✅ → P6 多智能体 ✅ → P7 MCP ✅ → P8 人工介入+安全 → P9 Vue3 前端 → P10 部署。

详见 [docs/P7-mcp-design.md](docs/P7-mcp-design.md)。

详见 [docs/PLAN.md](docs/PLAN.md)。
