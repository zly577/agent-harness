# Agent Harness

基于 **DeepAgents（LangChain + LangGraph）** 的自有 Harness 架构通用智能体系统。

默认环境是**用户这台电脑**：读写文件直接落在本机磁盘，只有执行代码才进隔离沙箱。
前端顶栏一个开关决定 `execute` 跑在沙箱里还是直接在本机 shell；默认档位下，
访问本机真实文件会弹人工审批。

## 技术栈

- Agent 框架：deepagents（LangChain + LangGraph）
- 后端：FastAPI + Pydantic v2
- 消息历史/短期记忆：SQLite（langgraph `AsyncSqliteSaver`/`AsyncSqliteStore`）
- 业务数据：MySQL 8.0（会话、后续 Skills/子智能体/审计）
- 模型：DeepSeek（deepseek-v4-flash，OpenAI 兼容接口）
- 文件读写：本地磁盘（`FilesystemBackend`，根目录 `<repo>/workspace`）
- 代码执行：OpenSandbox 沙箱容器（`opensandbox` SDK，每会话一个容器）
- 运行环境：conda `agent_env`（Python 3.12.13，deepagents 生态已预装）
- 前端：Vue3

## 工具分流

前端顶栏有一个**权限模式**开关，决定 `execute` 跑在哪：

| 模式 | 文件工具（ls/read_file/write_file/edit_file/delete/glob/grep） | `execute` | 本机 `/mnt/**` 访问 |
|------|------|------|------|
| **默认权限（沙箱内）** | 本地磁盘：工作区 `/workspace` + 本机 `/mnt/<盘符>/` | **OpenSandbox 容器**（工作目录 `/workspace`） | 读 / 写都**弹人工审批** |
| **完全访问**（LocalShellBackend） | 同上（完全一致） | **本机 shell**（直接在你电脑上跑） | 不审批 |

一句话：**默认权限 = 在沙箱里干活，要碰你电脑上的东西先问我；完全访问 = 就在你这台电脑上干活，不问了。**
完全访问是危险档位：默认关闭、切换要二次确认、后端重启自动回落。详见
[docs/P21-access-mode-design.md](docs/P21-access-mode-design.md)。

**默认环境是这台电脑**：文件工具直接在用户本机上读写，不经过沙箱；`execute` 才进容器
（默认权限模式下）。执行前后对 **工作区** 做文件同步：本地改动推送到容器（命令能读到），
容器产出拉回本地磁盘（用户能直接看到、下载）。注意**挂载点（`/mnt/...`）不进沙箱**——
要让代码在沙箱里跑，先把脚本/数据写到 `/workspace/` 下。

机制与取舍见 [docs/P11-opensandbox-hybrid-design.md](docs/P11-opensandbox-hybrid-design.md)、
[docs/P12-machine-access-design.md](docs/P12-machine-access-design.md) 与
[docs/P21-access-mode-design.md](docs/P21-access-mode-design.md)。

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

> 存储说明：**SQLite**（`backend/data/harness.sqlite`）存消息历史/短期记忆（langgraph checkpoint/store，跨进程重启不丢）；**MySQL**（`agent_harness.sessions`）存业务会话元信息；**本地磁盘**（`workspace/`）存模型读写的文件。详见 [docs/P2-storage-design.md](docs/P2-storage-design.md)。

## 沙箱配置

```powershell
# backend/.env
SANDBOX_ENABLED=true              # false = 文件工具仍可用本地磁盘，execute 返回"未启用"
SANDBOX_SYNC_ENABLED=true         # 执行前后是否同步本地↔容器文件
SANDBOX_DOMAIN=127.0.0.1:8080     # OpenSandbox 服务地址
SANDBOX_IMAGE=python:3.11
SANDBOX_ROOT=/workspace           # 容器工作目录 = 模型看到的路径前缀
# HARNESS_WORKSPACE=D:/Deepseek_Harness/agent-harness/workspace   # 本地磁盘根目录
```

OpenSandbox 服务没起也不影响文件读写：`execute` 会返回一句可读的错误而不是抛异常。

## 本机磁盘访问：默认环境 = 这台电脑

```powershell
# backend/.env
HARNESS_MACHINE_ACCESS=true       # 文件工具可直接读写本机真实文件
HARNESS_MACHINE_MOUNTS=           # 留空 = 自动挂载本机所有存在的盘符（c=C:/、d=D:/ …）
# 也可以只挂需要的目录（更收敛）：
# HARNESS_MACHINE_MOUNTS=proj=D:/Deepseek_Harness,home=C:/Users/Administrator
```

- 模型侧路径：`/mnt/<盘符>/...`（`ls /mnt` 能看到挂了哪些盘）；写操作默认走人工审批；
- `execute` 仍然只在沙箱容器里跑，**容器只镜像 `/workspace`**，看不到 `/mnt/*` 的真实文件；
- 想让某段代码在沙箱里跑：先用文件工具把内容写到 `/workspace/` 下，再 `execute`；
关掉它就回到只有 `/workspace` 的行为。

设计与坑详见 [docs/P12-machine-access-design.md](docs/P12-machine-access-design.md)。

## 安全约定

- 所有密钥只放 `.env`（已被 .gitignore 排除），绝不进代码/文档。
- 子智能体、Skill、MCP 的 YAML 配置一律经 Pydantic 校验后加载。
- 工作区文件被限制在 `HARNESS_WORKSPACE` 内（`virtual_mode=True` 拒绝 `..` 与越界绝对路径）；
  本机磁盘挂载点（`/mnt/<盘符>/`）同样各自关在该盘/该目录内，写操作默认需人工审批；
  代码执行只在一次性沙箱容器内进行，产出才同步回本地磁盘。

## 功能概览

- **对话与记忆**：多轮对话、消息历史持久化、上下文管理与摘要
- **文件与执行**：虚拟文件系统、本地磁盘读写、OpenSandbox 沙箱执行、本机 shell 执行
- **智能体编排**：子智能体注册与任务委派、Skills 技能加载、MCP 外部工具源接入
- **安全与治理**：人工审批中断与恢复、权限模式切换、账号认证与多租户隔离
- **可观测性**：SSE 流式输出、工具调用事件流、运行状态投影、意图标签

详细设计见 [docs/](docs/) 目录。
