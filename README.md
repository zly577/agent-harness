# Agent Harness

基于 **DeepAgents（LangChain + LangGraph）** 的自有 Harness 架构通用智能体系统。

当前进度：**P0 项目骨架**（FastAPI 空服务 + 健康检查 + 环境就绪）。

## 技术栈

- Agent 框架：deepagents（LangChain + LangGraph）
- 后端：FastAPI + Pydantic v2
- 数据库：PostgreSQL（本地 Docker Compose，P2 起使用）
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
│   │   ├── db/         # SQLAlchemy 会话脚手架（P2 启用）
│   │   ├── agent/      # Agent 核心层（P1 起填充）
│   │   ├── api/        # 路由（P1 起填充）
│   │   └── schemas/    # Pydantic 模型（P1 起填充）
│   ├── configs/        # skills / subagents / mcp 的 YAML 配置
│   └── tests/
├── docs/PLAN.md        # 项目计划书（基线文档）
└── docker-compose.yml  # 本地 PostgreSQL
```

## 快速开始（P0）

```powershell
# 1. 安装依赖（使用 conda 环境 agent_env，Python 3.12.13 满足 deepagents 的 ≥3.11 要求）
cd D:\Deepseek_Harness\agent-harness\backend
C:\Users\Administrator\anaconda3\envs\agent_env\python.exe -m pip install -r requirements-dev.txt

# 2. 配置环境变量
Copy-Item .env.example .env   # 填入 DEEPSEEK_API_KEY（P1 需要）

# 3. 启动
C:\Users\Administrator\anaconda3\envs\agent_env\python.exe -m uvicorn app.main:app --reload

# 4. 验证
Invoke-RestMethod http://127.0.0.1:8000/health
```

（可选）本地 PostgreSQL：

```powershell
docker compose up -d postgres   # P2 起需要
```

## 安全约定

- 所有密钥只放 `.env`（已被 .gitignore 排除），绝不进代码/文档。
- 子智能体、Skill、MCP 的 YAML 配置一律经 Pydantic 校验后加载。

## 路线图

P0 骨架 ✅ → P1 最小 Agent → P2 上下文持久化 → P3 虚拟FS沙箱 → P4 Skills → P5 中间件 → P6 多智能体 → P7 MCP → P8 人工介入+安全 → P9 Vue3 前端 → P10 部署。

详见 [docs/PLAN.md](docs/PLAN.md)。
