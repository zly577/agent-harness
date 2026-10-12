# Agent Harness

基于 DeepAgents、LangChain 和 LangGraph 的通用智能体应用，提供 Vue 3 网页界面与 FastAPI 后端。

支持多轮对话、SSE 流式输出、消息持久化、文件读写、子智能体、Skills、MCP 工具源、人工审批，以及按用户配置模型供应商。模型支持 DeepSeek、OpenAI 兼容接口和 Anthropic 协议。

## Docker 快速开始（推荐）

安装 Docker Engine + Compose v2，或 Docker Desktop（使用 Linux 容器），然后：

```sh
git clone https://github.com/zly577/agent-harness.git
cd agent-harness
python deploy/init-env.py
docker compose up -d --build
```

Windows 上若 Python 命令为 `py`，第三步用 `py deploy/init-env.py`。没有 Python 也可用 Docker 生成配置，见 [部署手册](deploy/README-docker.md)。

浏览器打开 **http://localhost:8088**。用根目录 `.env` 中的 `BOOTSTRAP_ADMIN_EMAIL` 和 `BOOTSTRAP_ADMIN_PASSWORD` 登录，再在界面「模型」里添加自己的供应商、API Key 和模型名称。

基础部署包含前端、后端、MySQL，支持聊天与文件读写；**代码执行还需 OpenSandbox**：

```sh
docker compose -f docker-compose.yml -f docker-compose.sandbox.yml up -d --build
```

OpenSandbox 服务通过 Docker socket 创建临时执行容器，建议在专用机器部署。沙箱镜像、网络与公网部署注意事项见 [部署手册](deploy/README-docker.md)。Docker 部署默认禁用「完全访问」模式，避免命令接触后端密钥和其他用户的数据。

> 本仓库已提供源码构建部署文件。预构建镜像尚未发布到镜像仓库，首次启动会下载基础镜像并安装依赖。构建需要可访问 Docker Hub、PyPI 和 npm。

## 架构与数据流

```text
浏览器
  └─ Web / Nginx :8088
      ├─ Vue 静态页面
      └─ /api/* → FastAPI（内部 :8000，单 worker）
          ├─ MySQL：用户、会话元信息、供应商、MCP、审计与工具日志
          ├─ SQLite：LangGraph 消息历史、checkpoint 与 store
          ├─ workspace/<user_id>：用户文件与 Agent.md 长期记忆
          └─ OpenSandbox（可选）：每会话执行容器，执行前后同步工作区
```

Docker 中的文件工作区位于持久化卷，**不是访问网页那个人的电脑磁盘**。Windows 本机运行时的 `/mnt/c`、`/mnt/d` 不会自动出现在 Docker 部署中。

后端目前有进程内的 Agent、审批和运行状态缓存，并使用 SQLite；部署保持一个后端实例、一个 worker。多实例扩容需要先改造共享状态与存储。

## 目录与职责

| 路径 | 职责 |
|---|---|
| `frontend/src/` | Vue 界面、登录、模型/MCP 管理、流式事件展示 |
| `backend/app/main.py` | 应用入口、生命周期、自动迁移、健康检查 |
| `backend/app/api/` | 认证、会话、聊天、文件、审批等 HTTP 接口 |
| `backend/app/agent/` | Agent 构建、模型调用、上下文、执行、权限、Skills/子智能体/MCP |
| `backend/app/core/` | 配置、密码/JWT、密钥加密、工作区路径 |
| `backend/app/db/` | MySQL 模型、连接、管理员引导与存量回填 |
| `backend/app/schemas/` | 请求、响应和配置校验 |
| `backend/configs/` | Skills、子智能体、MCP 的 YAML/Markdown 配置 |
| `backend/alembic/` | 数据库结构迁移 |
| `backend/tests/` | 后端功能与回归测试 |
| `deploy/` | Docker 部署说明、配置生成、Nginx/OpenSandbox 配置；历史裸机脚本 |
| `docs/` | 架构说明与历史阶段设计 |

## 配置与数据

- **根目录 `.env`**：Docker Compose 部署配置，由 `deploy/init-env.py` 生成。
- **`backend/.env`**：本机开发配置，从 `backend/.env.example` 复制；不进入 Docker 镜像。
- **用户模型 Key**：通过网页配置，Fernet 加密后保存到 MySQL。
- **持久化卷**：MySQL、SQLite、工作区、后端日志；重建容器保留数据。
- **密钥备份**：务必保存根目录 `.env`；丢失 `FERNET_KEY` 会无法解密已有供应商 Key。
- **注册**：Docker 默认关闭自助注册；需要时将 `ALLOW_REGISTRATION=true`，重新运行启动命令。

## 本机开发

Python 3.12、Node.js 22、MySQL 8.0。先创建 `agent_harness` 数据库，按实际账户填写 `backend/.env` 的 `MYSQL_URL`。

```sh
cd backend
python -m venv .venv
# 激活虚拟环境后：
python -m pip install -r requirements-dev.txt
# 复制 .env.example 为 .env，填写数据库连接
python -m uvicorn app.main:app --reload --host 127.0.0.1 --port 8000
```

另开终端：

```sh
cd frontend
npm ci
npm run dev
```

开发界面为 http://localhost:5173。开发环境可自动生成本地密钥；生产环境必须显式配置 `JWT_SECRET`、`FERNET_KEY` 与管理员密码。

## 执行与权限

| 部署方式 | 文件工具 | execute |
|---|---|---|
| Docker 基础部署 | 用户持久化工作区 | 未启用 |
| Docker + OpenSandbox | 用户持久化工作区 | 临时沙箱容器，工作区双向同步 |
| 本机开发，sandbox 模式 | 工作区；按配置可额外挂载本机目录 | OpenSandbox |
| 本机开发，full 模式 | 同上 | 后端所在机器的 shell |

`HARNESS_MACHINE_ACCESS` 控制文件挂载，`HARNESS_ALLOW_FULL_ACCESS` 控制能否切换 full 模式；二者控制不同能力。Docker 默认关闭两者。人工审批的当前边界是访问 `/mnt/**`，工作区内读写不弹审批。

## 文档导航

- [Docker 部署、升级、备份与排障](deploy/README-docker.md)
- [项目结构与后续整理建议](docs/PROJECT-OVERVIEW.md)
- [存储设计](docs/P2-storage-design.md)
- [OpenSandbox 混合后端](docs/P11-opensandbox-hybrid-design.md)
- [认证与多租户](docs/P18-auth-multitenant-multiprovider-plan.md)
- [迁移与启动](docs/P18-migration-and-startup.md)
- [MCP](docs/P19-mcp.md)
- [权限模式](docs/P21-access-mode-design.md)
- [运行状态展示](docs/P22-run-visibility-design.md)

`docs/P*.md` 与 `CODE-CATALOG.md` 记录历史设计，部分描述早于当前实现；启动与部署以本 README 和 Docker 手册为准。`deploy/deploy.sh`、systemd 配置和 `一键启动.bat` 保留给历史环境，不是 Docker 部署入口。
