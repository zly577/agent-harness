# Docker 部署手册

## 1. 部署内容

基础模式：Nginx/Vue 前端 + FastAPI 后端 + MySQL 8.0。可选模式额外启动 OpenSandbox 服务，由它创建会话执行容器。

不需要在宿主机安装 Conda、Node、MySQL，也不需要运行任何 .bat 或 systemd 安装脚本。首次构建需要从 Docker Hub、PyPI、npm 下载依赖。

支持 Docker Engine + Compose v2（Linux）或 Docker Desktop（Windows/macOS，Linux 容器）。建议至少 4GB 可用内存；启用沙箱时每个沙箱默认还有 2Gi 内存额度，应按并发增加资源。

## 2. 获取代码与生成配置

```sh
git clone https://github.com/zly577/agent-harness.git
cd agent-harness
python deploy/init-env.py
```

Windows 如果没有 `python` 命令但装了 Python Launcher，使用 `py deploy/init-env.py`。

**只有 Docker、没有 Python** 时，在仓库根目录运行以下任一命令。

PowerShell：

```powershell
docker run --rm --mount "type=bind,source=$PWD,target=/project" -w /project python:3.12-slim python deploy/init-env.py
```

Linux/macOS：

```sh
docker run --rm --user "$(id -u):$(id -g)" --mount "type=bind,source=$(pwd),target=/project" -w /project python:3.12-slim python deploy/init-env.py
```

生成器创建根目录 `.env`，包含随机数据库密码、JWT 签名密钥、Fernet 主密钥、管理员密码与 OpenSandbox API Key；已有文件会拒绝覆盖。不要提交或分享真实 `.env`。

打开该文件确认：

| 配置 | 作用 |
|---|---|
| `BOOTSTRAP_ADMIN_EMAIL` | 初始管理员邮箱，可改成自己的 |
| `BOOTSTRAP_ADMIN_PASSWORD` | 随机生成的初始密码，请保存 |
| `WEB_PORT` | 默认 8088，可改为未占用端口 |
| `WEB_BIND_IP` | 默认 127.0.0.1，仅本机访问 |
| `ALLOW_REGISTRATION` | 默认 false，需要公开自助注册时改 true |
| `TAVILY_API_KEY` | 可选，启用联网搜索 |
| `MCP_ENABLED` | 默认 true，用户自行添加 MCP 工具源；不预置已启用服务器 |

保持自动生成的 `MYSQL_PASSWORD` 使用十六进制字母数字。它会拼入连接 URL，手动设置包含 `@ / : % # $` 等字符的密码需要额外 URL 编码和 Compose 转义，不建议直接替换。

根目录 `.env` 专用于 Compose；`backend/.env` 属于本机开发，**不会打进镜像，也不会用于 Docker 启动**。添加其他后端配置时，需要同时在 Compose 的 backend.environment 显式传入。

## 3. 启动基础服务

Windows 个人电脑如果需要自动读取全部当前可用的本地硬盘/移动盘、切换完全访问、打开本机工作区，可改用 `powershell -NoProfile -ExecutionPolicy Bypass -File .\deploy\start-windows.ps1`。默认文件工具只读磁盘，完全访问可写并使用后端容器的 LocalShell；详细说明见 [README 的 Windows 个人部署](../README.md#windows-个人部署自动读取本机磁盘)。其他平台及多人部署使用本节的基础命令。

```sh
docker compose up -d --build
docker compose ps
```

首次安装依赖、MySQL 初始化和健康检查需要时间。顺序为：MySQL 可用 → 后端迁移数据库并引导管理员 → 前端启动。后端使用非 root 用户、一个 worker。

打开 **http://localhost:8088**。使用根目录 `.env` 中的管理员账户登录，然后：

1. 在「模型」中添加供应商、Base URL、API Key、模型名称并设为默认。
2. 发一条消息验证模型连接。
3. 需要联网搜索时填写 `TAVILY_API_KEY`，再运行 `docker compose up -d`。
4. MCP 默认启用，在界面添加自己的工具源即可。旧 `.env` 若仍为 `MCP_ENABLED=false`，需改为 true 并重建容器。stdio MCP 所需命令/依赖必须装在后端镜像内；访问远端 HTTP MCP 不需要把对方服务装进镜像。

运行自动冒烟检查（无需模型 Key）：

```sh
python deploy/smoke-test.py
```

它检查页面、健康接口、管理员登录与当前用户，并确认注册策略和 full 模式禁用状态。它不会调用模型或修改用户文件。

基础模式支持对话、消息历史和工作区文件工具，`execute` 会提示沙箱未启用。启用完整代码执行见下一节。

## 4. 启用 OpenSandbox

在根目录 `.env` 中保留生成的 `SANDBOX_API_KEY`。运行：

```sh
docker compose -f docker-compose.yml -f docker-compose.sandbox.yml up -d --build
docker compose -f docker-compose.yml -f docker-compose.sandbox.yml ps
```

叠加文件会自动设置 `SANDBOX_ENABLED=true` 和 `SANDBOX_DOMAIN=opensandbox:8080`，不用手动更改这两项。后续启动、更新、关闭也使用同样的两个 `-f` 参数。

OpenSandbox 的生命周期服务不向宿主机发布管理端口；后端经 Compose 内网连接它。执行容器由它通过 Docker socket 创建，按 OpenSandbox 官方的容器代理方案，经过 `host.docker.internal` 访问执行端点。服务镜像固定为官方推荐修复此网络路径的 `opensandbox/server:release-1.1.0`。

预先下载沙箱运行镜像，可减少首次执行等待：

```sh
docker pull python:3.11
docker pull opensandbox/execd:v1.1.0
```

登录后让智能体「在 /workspace 写一个小 Python 脚本，执行它并展示输出」验证完整执行路径。镜像预拉、SDK 版本兼容、容器创建和文件回传都需要在目标机器实际验证，`/health` 成功不代表这些能力已经正常。

**宿主机权限和网络：**

- 只有 OpenSandbox 服务挂 Docker socket，后端应用没有该权限；socket 允许管理宿主机容器，因此沙箱服务应部署在专用机器。
- OpenSandbox 默认 bridge 运行时可能在宿主机动态发布沙箱端口。公网部署需按官方配置收窄 `[docker].publish_host` 到容器可达的宿主机网关 IP，并验证防火墙规则；具体 IP 依目标 Docker 网络而定。不要把执行端点直接暴露到公网。
- 文件同步仅覆盖用户工作区；不会自动挂载宿主机所有目录或网页访问者的本地硬盘。
- `python:3.11` 沙箱只带基础 Python。需要 pandas、文档工具等时构建自己的沙箱镜像，把根目录 `.env` 的 `SANDBOX_IMAGE` 改为该镜像名。沙箱 Python 和后端 Python 是两套环境。
- 普通服务器 Compose 不启用「完全访问」；Windows 个人入口允许切换。该模式绕过 OpenSandbox，在后端容器里执行命令，不能当作多人部署的隔离方案。

也可以使用**已有或独立机器上的 OpenSandbox**：只启动基础 Compose，把根目录 `.env` 设置为：

```dotenv
SANDBOX_ENABLED=true
SANDBOX_DOMAIN=你的沙箱服务地址:8080
SANDBOX_PROTOCOL=http
SANDBOX_API_KEY=与服务端一致的密钥
```

然后运行 `docker compose up -d`。同机服务可用 `host.docker.internal:8080`，但服务端必须监听后端容器能访问的接口；`127.0.0.1` 在后端容器内表示后端容器自身。

参考：[OpenSandbox 单机网络说明](https://open-sandbox.ai/architecture/network/single-host-network)、[官方 Compose 示例](https://github.com/opensandbox-group/OpenSandbox/blob/main/server/docker-compose.example.yaml)、[配置参考](https://open-sandbox.ai/getting-started/configuration)。

## 5. 日常管理

基础模式：

```sh
docker compose logs -f backend
docker compose stop
docker compose start
docker compose down
```

`down` 删除服务容器和 Compose 网络，保留数据卷。**不要执行 `docker compose down -v`，它会删除持久化数据。** OpenSandbox 动态创建的执行容器不属于 Compose 服务；它们依靠沙箱关闭或 TTL 回收，不应假设 Compose down 会立即删除它们。

改 `.env` 后执行 `docker compose up -d` 使环境变量生效，单独 `restart` 不会重新读取新配置。

更新：

```sh
# 先按下一节备份
git pull
docker compose up -d --build
```

启用沙箱时，以上 Compose 命令都加上两个 `-f` 参数。后端启动会自动执行 Alembic 迁移；旧版本回退前必须确认数据库迁移兼容，必要时从整套备份恢复。

## 6. 数据与备份

| Compose 卷 | 内容 |
|---|---|
| `mysql_data` | 用户、会话元信息、模型/MCP 配置、审计等业务数据 |
| `backend_data` | SQLite 消息历史、checkpoint、store |
| `workspace_data` | 按 user_id 划分的工作区与长期记忆 |
| `backend_logs` | 后端滚动日志 |
| `sandbox_data`（可选） | OpenSandbox 服务状态；不替代用户工作区备份 |

Windows 个人入口把 `/app/workspace` 改为绑定仓库的 `workspace/`；此时需备份本机整个 `workspace` 目录，旧 `workspace_data` 卷只保留切换前的数据。首次切换会停止旧后端并从默认项目名的旧卷复制数据，不覆盖已有文件，也不删除旧卷。

默认项目名为 `agent-harness`，实际卷名通常带此前缀；换项目名会使用另一组卷，不会自动迁移原数据。

最简单的一致性备份：先停止服务，再归档所需卷和根目录 `.env`。下面为 Linux/macOS 示例：

```sh
mkdir -p backups
docker compose stop
for volume in mysql_data backend_data workspace_data backend_logs; do
  docker run --rm --mount "type=volume,source=agent-harness_${volume},target=/data,readonly" --mount "type=bind,source=$(pwd)/backups,target=/backup" alpine:3.22 tar -czf "/backup/${volume}.tar.gz" -C /data .
done
cp .env backups/deployment.env
docker compose start
```

启用沙箱时，停止/启动使用两个 `-f` 参数，卷列表加上 `sandbox_data`。本命令假设默认项目名，执行前用 `docker volume ls` 确认实际名称。备份目录含密钥与用户文件，需限制访问并复制到另一台机器。

恢复时先停止服务，把对应归档解压回**对应卷**，恢复同一份 `.env`，然后启动。MySQL 的物理卷备份应恢复到兼容的 MySQL 8.0 版本。数据库、SQLite、workspace 和 Fernet 密钥必须配套；不要只拷贝聊天 SQLite 文件。

已有 Windows 裸机数据不会自动导入 Docker。迁移要先停旧后端，导出/导入 MySQL、复制 `backend/data` 和整个 workspace 到目标卷，并保留原 `FERNET_KEY`。JWT 可沿用原值以保持已有 token。旧系统已存在管理员时，新 `BOOTSTRAP_ADMIN_PASSWORD` 不会重置其口令。

## 7. 给其他人部署和对外发布

你需要提交这些文件：前后端 Dockerfile、两套 Compose、.dockerignore、根目录 .env.example、deploy/init-env.py、deploy/docker 配置、部署说明，以及禁止 full 的代码修改。**不要提交生成的 .env、数据卷、workspace、API Key。**

其他人只需克隆更新后的仓库、生成各自的配置、启动 Compose，并在网页填自己的模型信息。他们不需要你本机的盘符、Conda 环境或 .bat。

这套文件目前提供源码构建。若希望用户连构建都不用做，下一步是把前后端镜像发布到 GHCR 或 Docker Hub，按版本打标签，再提供使用 `image:` 的发布 Compose。发布稳定版本前建议锁定后端依赖和镜像 digest，并明确项目许可证；当前仓库没有许可证，不应替你擅自选择。

局域网/公网服务把 `WEB_BIND_IP=0.0.0.0`，并按实际地址更新 `CORS_ORIGINS`。公网推荐在入口前配置 HTTPS 反向代理，仍保持 MySQL 与后端端口不对外开放。只需开放最终的网页入口。每个人用自己的账户和供应商 Key；开启注册时也要明确预期访问人群。

## 8. 排障与验证边界

- **拉镜像超时**：检查宿主机 Docker Hub 网络、DNS 和代理，尤其 `auth.docker.io`；这发生在应用构建之前。不要把密钥写进镜像来解决下载问题。
- **backend 不健康**：查看 `docker compose logs --tail 100 backend mysql`，检查数据库和环境变量，等待 MySQL 首次初始化。
- **改数据库密码后连接失败**：MySQL 初始化变量只作用于空数据卷；需要在数据库中实际修改密码并同步 .env。不要删卷来修复登录问题。
- **页面能开但模型不可用**：登录后配置个人模型，根目录没有系统级 DeepSeek Key 是正常的。
- **SSE 不流式**：检查外层反向代理也关闭缓冲并增加读取超时；项目自带 Nginx 已配置。
- **管理员密码无效**：引导密码只用于 users 表为空时创建账户；已有账户需通过改密码接口处理。
- **execute 不可用**：基础模式默认关闭；沙箱模式检查 OpenSandbox 日志、镜像下载和代理端点连通性。
- **改 full 返回 400**：Docker 部署主动禁用，使用 OpenSandbox 执行代码。

GitHub 的 `Docker smoke` workflow 验证基础镜像构建、服务启动、健康检查与登录，并静态校验沙箱叠加配置。它不调用真实模型，也不宣称验证 OpenSandbox 端到端执行。

GitHub CI 已验证基础 Docker 构建、启动与登录。Windows 入口在 CI 中使用替身 Docker 检查配置、升级命令和协议路径校验，不会挂载 CI 磁盘或打开资源管理器；真实桌面协议打开与 OpenSandbox 端到端执行需要在部署者电脑上验证。
