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

OpenSandbox 服务通过 Docker socket 创建临时执行容器。普通服务器 Compose 部署禁用「完全访问」；下面的 Windows 个人部署入口允许用户手动切换。网络与公网部署注意事项见 [部署手册](deploy/README-docker.md)。

> 本仓库已提供源码构建部署文件。预构建镜像尚未发布到镜像仓库，首次启动会下载基础镜像并安装依赖。构建需要可访问 Docker Hub、PyPI 和 npm。

## Windows 个人部署：自动读取本机磁盘

如果部署者希望智能体读取自己电脑上的磁盘，安装并启动 Docker Desktop（Linux 容器），在仓库根目录执行：

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File .\deploy\start-windows.ps1
```

此入口不使用 `.bat`，也不要求宿主机安装 Python。它自动识别当前可用的本地硬盘和移动盘，生成支持双模式的挂载配置；首次启动用 Docker 生成 `.env`，然后构建并启动应用。已有 `.env` 不会被覆盖。

- C、D 等盘在模型侧对应 `/mnt/c`、`/mnt/d`；可让智能体「查看 `/mnt/d/资料`」。
- **默认模式**：工作区可读写，文件工具只读磁盘；即使 OpenSandbox 关闭也能读取磁盘。
- **完全访问**：在顶栏确认后启用，文件工具可修改/删除已挂载磁盘中的文件，命令由 LocalShell 执行；不依赖 OpenSandbox 是否启用。重启回到默认模式。
- 为支持运行时切换，Docker 挂载本身可写，默认只读由应用文件后端强制实施；不能把该模式当作操作系统级只读隔离。Windows 系统权限仍然有效。
- 该目录对已有应用账户共享，适合个人部署。此入口关闭自助注册；不要给不信任的人登录或把入口公开到公网。
- 自动挂载的是**运行脚本那台电脑**的磁盘，不是远程网页访问者的电脑。网络盘、光驱和未就绪设备不自动挂载。
- 新增/移除磁盘后重新运行入口更新配置；生成的 `docker-compose.host.generated.yml` 已被 Git 忽略，不要提交。

需要同时启用沙箱代码执行，追加 `-Sandbox`：

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File .\deploy\start-windows.ps1 -Sandbox
```

默认模式的执行工具进入本机 Docker 上的 OpenSandbox，沙箱只同步 `/workspace`，需先把执行所需脚本/数据复制到工作区。完全访问模式的 LocalShell 则运行在后端 Linux 容器里，可通过 `/host/c`、`/host/d` 等真实挂载路径处理宿主机文件；它不执行 Windows 系统的 cmd/PowerShell 命令。仅浏览器切换权限才启用 full，不会在启动时自动切换。

### 打开工作区目录

「文件」面板新增 **打开目录** 按钮，打开当前登录用户的工作区：

- Windows 直接运行后端：打开这台电脑的资源管理器。
- Windows Docker 个人入口：工作区绑定到仓库的 `workspace/<user_id>`，入口为当前 Windows 用户注册 `agent-harness-workspace` 协议；点击后允许浏览器打开对应目录。
- 目录协议通过 Windows Script Host 隐藏启动路径校验，只显示资源管理器，不弹 PowerShell 窗口。更新旧版本后重新运行 `deploy/register-workspace.ps1` 刷新协议注册即可，无需重建容器。
- 单独注册时会识别当前 Docker 后端实际挂载的工作区；也可显式指定 `-WorkspaceRoot "D:\你的部署目录\workspace"`，避免多份项目目录之间打开错位置。个人启动入口会绑定本次部署的工作区。
- 远程服务器或另一台访问电脑：不会打开服务器桌面或把服务器磁盘变成访问者本机目录，继续使用下载功能。

启动入口首次切换到本机工作区时，停止旧后端写入，把默认项目名 `agent-harness` 下旧 `workspace_data` 卷的数据复制进本机 `workspace`，不覆盖已有文件、不删除旧卷。使用了其他 Compose 项目名时请先手动迁移对应工作区卷。个人入口以后备份 `workspace/` 目录；不要只备份旧卷。

如果不需要桌面协议注册，启动时加 `-SkipDesktopIntegration`。协议注册指向当前仓库目录；移动仓库后重新运行入口。恢复为普通服务器部署可用基础 Compose；那会重新使用原工作区卷，新产生的本机工作区文件不会自动复制回原卷。

后续查看状态、日志或关闭，使用相同入口；启用了沙箱时同样保留 `-Sandbox`：

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File .\deploy\start-windows.ps1 -Action Status
powershell -NoProfile -ExecutionPolicy Bypass -File .\deploy\start-windows.ps1 -Action Logs
powershell -NoProfile -ExecutionPolicy Bypass -File .\deploy\start-windows.ps1 -Action Down
```

只想预览挂载目录、不启动容器，用 `-ConfigureOnly`。普通服务器/多人部署继续使用上面的基础 Compose 命令，只开放用户工作区。

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

普通 Docker 部署的工作区位于持久化卷，**不是访问网页那个人的电脑磁盘**。Windows 个人入口把工作区绑定到本机目录，同时把部署者的磁盘映射为 `/mnt/c`、`/mnt/d`，按当前权限模式控制文件修改；普通 Compose 启动不会自动挂载磁盘。

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
- **MCP**：默认启用接入能力，但不预置可用工具；用户在「MCP」界面添加自己的服务器。已有 `.env` 的 `MCP_ENABLED=false` 会保留，请手动改为 true；Windows 个人入口已强制启用。

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
| Windows Docker 个人入口，默认模式 | 工作区可写，宿主磁盘只读 | OpenSandbox；未开启时不可执行 |
| Windows Docker 个人入口，完全访问 | 工作区与宿主磁盘可写 | 后端 Linux 容器内 LocalShell，不依赖 OpenSandbox |
| 本机开发，sandbox 模式 | 工作区；按配置可额外挂载本机目录 | OpenSandbox |
| 本机开发，full 模式 | 同上 | 后端所在机器的 shell |

`HARNESS_MACHINE_ACCESS` 控制文件挂载，`HARNESS_ALLOW_FULL_ACCESS` 控制能否切换 full 模式。普通服务器 Compose 关闭两者，Windows 个人入口开启两者并以 sandbox 模式启动。默认模式中挂载盘的写入会被文件后端拒绝，读操作保留人工审批；工作区内读写不弹审批。完全访问不弹审批。

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


### 邮箱验证码注册

注册时填写邮箱、8–128 位密码和 6 位邮箱验证码，昵称选填。验证码 10 分钟有效，每个邮箱发送间隔 60 秒，输错 5 次后需要重新获取；注册成功后验证码立即失效，并自动登录。已有账号（包括 `admin@local`）仍可用原密码登录。

部署前设置 `ALLOW_REGISTRATION=true`，并配置 `SMTP_HOST`、`SMTP_PORT`、`SMTP_USERNAME`、`SMTP_PASSWORD`（邮箱服务的授权码）和 `SMTP_FROM`（允许发信的邮箱地址）。587 端口默认启用 STARTTLS；使用 465 端口时设置 `SMTP_SSL=true`。本地运行填写 `backend/.env`，Docker 部署填写根目录 `.env` 后重建后端容器。

启动时 Alembic 自动创建 `registration_codes` 表；手动部署也可在 backend 目录执行 `alembic upgrade head`。未配置邮件服务时发送验证码会提示暂不可用，不会绕过邮箱验证。上线前需使用自己的 SMTP 账号完成一次真实收信验证。


常见发件邮箱预设（QQ、163、企业邮箱、微软 OAuth）及连接/收信验证步骤见 [邮件服务配置](deploy/README-smtp.md)。
