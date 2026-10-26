# Docker 部署手册

## 1. 部署内容

默认部署完整的四个服务：Nginx/Vue 前端、FastAPI 后端、MySQL 8.0 和 OpenSandbox。OpenSandbox 创建每会话的临时执行容器，支持沙箱代码执行，无需额外启用步骤。

不需要在宿主机安装 Conda、Node、MySQL，也不需要运行任何 .bat 或 systemd 安装脚本。首次构建需要从 Docker Hub、PyPI、npm 下载依赖。

支持 Docker Engine + Compose v2（Linux）或 Docker Desktop（Windows/macOS，Linux 容器）。建议至少 4GB 可用内存；每个执行沙箱默认还有 2Gi 内存额度，应按并发增加资源。

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
| `ALLOW_REGISTRATION` | 默认 true，允许直接注册；设为 false 关闭公开注册 |
| `REGISTRATION_EMAIL_VERIFICATION` | 默认 false；设为 true 时注册需要邮箱验证码，并须配置 SMTP |
| 联网搜索 | 无需 API key，Bing RSS 优先，DuckDuckGo、全球 Bing、360、百度自动备用 |
| `TAVILY_API_KEY` | 可选：优先使用 Tavily 官方结构化 API（按供应商套餐计费），失败自动回退；[API 文档](https://docs.tavily.com/documentation/api-reference/endpoint/search) |
| `WEB_SEARCH_TIMEOUT_SECONDS` | 搜索总时限，默认 30 秒，同时受 MCP 工具超时上限约束 |
| `WEB_SEARCH_PROVIDER_TIMEOUT_SECONDS` | 每个搜索源的最长等待时间，默认 5 秒，给备用源预留时间 |
| `WEB_SEARCH_PROXY` | 可选 HTTP 代理，默认直连；Docker 使用容器可达的地址，例如 `http://host.docker.internal:7890` |
| `MCP_ENABLED` | 默认 true，用户自行添加 MCP 工具源；不预置已启用服务器 |

保持自动生成的 `MYSQL_PASSWORD` 使用十六进制字母数字。它会拼入连接 URL，手动设置包含 `@ / : % # $` 等字符的密码需要额外 URL 编码和 Compose 转义，不建议直接替换。

根目录 `.env` 专用于 Compose；`backend/.env` 属于本机开发，**不会打进镜像，也不会用于 Docker 启动**。添加其他后端配置时，需要同时在 Compose 的 backend.environment 显式传入。

## 3. 启动完整服务

Windows 个人电脑如果需要自动读取全部当前可用的本地硬盘/移动盘、切换完全访问、打开本机工作区，可改用 `powershell -NoProfile -ExecutionPolicy Bypass -File .\deploy\start-windows.ps1`。默认文件工具只读磁盘，完全访问可写并使用后端容器的 LocalShell；详细说明见 [README 的 Windows 个人部署](../README.md#windows-个人部署自动读取本机磁盘)。其他平台及多人部署使用本节的基础命令。

```sh
docker compose up -d --build
docker compose ps
```

首次安装依赖、MySQL 初始化和健康检查需要时间。顺序为：MySQL 与 OpenSandbox 健康 → 后端迁移数据库并引导管理员 → 前端启动。`docker compose ps` 应列出 `mysql`、`opensandbox`、`backend`、`web` 四个服务。后端使用非 root 用户、一个 worker。

打开 **http://localhost:8088**。使用根目录 `.env` 中的管理员账户登录，然后：

1. 在「模型」中添加供应商、Base URL、API Key、模型名称并设为默认。
2. 发一条消息验证模型连接。
3. 运行 `docker compose up -d`；联网搜索无需密钥，优先解析 Bing RSS 结构化结果，DuckDuckGo、全球 Bing、360、百度自动备用。无密钥端点仍可能限流或返回不够精确的结果；持续使用可配置 `TAVILY_API_KEY` 接入官方 API。每个搜索源有独立超时，总调用默认最多 30 秒。修改搜索配置后重新运行 `docker compose up -d backend`。
4. MCP 默认启用，在界面添加自己的工具源即可。旧 `.env` 若仍为 `MCP_ENABLED=false`，需改为 true 并重建容器。stdio MCP 所需命令/依赖必须装在后端镜像内；访问远端 HTTP MCP 不需要把对方服务装进镜像。

运行自动冒烟检查（无需模型 Key）：

```sh
python deploy/smoke-test.py
```

它检查页面、健康接口、管理员登录、注册与权限策略，并从后端实际创建临时沙箱、运行 Python、核对输出，最后销毁测试容器。它不会调用模型或修改用户文件。Windows 个人部署运行 `python deploy/smoke-test.py --personal`，以检查允许手动切换 full 的个人部署策略。

Windows 个人部署点击侧栏「新建工作区」会调用 Windows 原生文件夹选择窗口（带地址栏、导航和新建文件夹功能）。`start-windows.ps1` 会自动注册本机协议；已有部署更新后运行 `powershell -NoProfile -ExecutionPolicy Bypass -File .\deploy\register-workspace.ps1` 更新注册。首次使用请允许浏览器打开 Agent Harness。选中的宿主目录会自动转换成对应容器挂载路径；未挂载的目录无法选择。目录回传使用短时一次性票据，不传递登录令牌。

右上角「文件」面板与当前选中的工作区绑定，切换后会刷新文件列表。「打开目录」使用相同的会话/工作区目录，通过实际 Docker 挂载映射回 Windows 路径；默认工作区和自选磁盘目录都支持。

普通 Docker 部署或远程浏览器访问使用网页目录选择器，可在当前用户的持久化工作区中新建文件夹。原生窗口未出现时，点击等待界面的「使用网页选择」也可继续。

默认部署支持对话、消息历史、工作区文件工具和 OpenSandbox 代码执行。页面或 `/health` 可用不能代替完整冒烟检查。

## 4. 默认启用的 OpenSandbox

在根目录 `.env` 中保留生成的 `SANDBOX_API_KEY`。标准启动命令已经包含 OpenSandbox：

```sh
docker compose up -d --build
docker compose ps
```

基础 Compose 固定传入 `SANDBOX_ENABLED=true`、`SANDBOX_DOMAIN=opensandbox:8080` 和 `SANDBOX_PROTOCOL=http`，不会被旧 `.env` 中的 `false` 或宿主机地址覆盖。Windows 入口不再要求 `-Sandbox`。旧 `docker-compose.sandbox.yml` 和 `-Sandbox` 参数只为兼容旧命令保留；后续启动、更新、关闭都不再需要它们。

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

标准 Docker 部署统一使用内置 OpenSandbox。如需接入独立服务，需自行提供 Compose 覆盖文件调整后端地址、协议和密钥，并重新验证容器网络及执行链路；仅修改根目录 `.env` 的 `SANDBOX_DOMAIN` 不会改变标准部署。

参考：[OpenSandbox 单机网络说明](https://open-sandbox.ai/architecture/network/single-host-network)、[官方 Compose 示例](https://github.com/opensandbox-group/OpenSandbox/blob/main/server/docker-compose.example.yaml)、[配置参考](https://open-sandbox.ai/getting-started/configuration)。

## 5. 日常管理

四个服务使用同一组管理命令：

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

从旧版升级时保留原 `.env` 和数据卷。确认 `.env` 中有非空 `SANDBOX_API_KEY`；若旧配置缺少这一项，只补充随机密钥，不要重新生成整份配置。后端启动会自动执行 Alembic 迁移；旧版本回退前必须确认数据库迁移兼容，必要时从整套备份恢复。

## 6. 数据与备份

| Compose 卷 | 内容 |
|---|---|
| `mysql_data` | 用户、会话元信息、模型/MCP 配置、审计等业务数据 |
| `backend_data` | SQLite 消息历史、checkpoint、store |
| `workspace_data` | 按 user_id 划分的工作区与长期记忆 |
| `backend_logs` | 后端滚动日志 |
| `sandbox_data` | OpenSandbox 服务状态；不替代用户工作区备份 |

Windows 个人入口把 `/app/workspace` 改为绑定仓库的 `workspace/`；此时需备份本机整个 `workspace` 目录，旧 `workspace_data` 卷只保留切换前的数据。首次切换会停止旧后端并从默认项目名的旧卷复制数据，不覆盖已有文件，也不删除旧卷。

默认项目名为 `agent-harness`，实际卷名通常带此前缀；换项目名会使用另一组卷，不会自动迁移原数据。

最简单的一致性备份：先停止服务，再归档所需卷和根目录 `.env`。下面为 Linux/macOS 示例：

```sh
mkdir -p backups
docker compose stop
for volume in mysql_data backend_data workspace_data backend_logs sandbox_data; do
  docker run --rm --mount "type=volume,source=agent-harness_${volume},target=/data,readonly" --mount "type=bind,source=$(pwd)/backups,target=/backup" alpine:3.22 tar -czf "/backup/${volume}.tar.gz" -C /data .
done
cp .env backups/deployment.env
docker compose start
```

本命令假设默认项目名，执行前用 `docker volume ls` 确认实际名称。备份目录含密钥与用户文件，需限制访问并复制到另一台机器。

恢复时先停止服务，把对应归档解压回**对应卷**，恢复同一份 `.env`，然后启动。MySQL 的物理卷备份应恢复到兼容的 MySQL 8.0 版本。数据库、SQLite、workspace 和 Fernet 密钥必须配套；不要只拷贝聊天 SQLite 文件。

已有 Windows 裸机数据不会自动导入 Docker。迁移要先停旧后端，导出/导入 MySQL、复制 `backend/data` 和整个 workspace 到目标卷，并保留原 `FERNET_KEY`。JWT 可沿用原值以保持已有 token。旧系统已存在管理员时，新 `BOOTSTRAP_ADMIN_PASSWORD` 不会重置其口令。

## 7. 给其他人部署和对外发布

你需要提交这些文件：前后端 Dockerfile、默认 Compose、兼容旧命令的沙箱叠加文件、.dockerignore、根目录 .env.example、deploy/init-env.py、deploy/docker 配置、部署说明，以及禁止 full 的代码修改。**不要提交生成的 .env、数据卷、workspace、API Key。**

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
- **execute 不可用**：默认部署应启动 OpenSandbox；检查四个服务状态、`docker compose logs opensandbox backend`、镜像下载和代理端点连通性，再运行完整冒烟检查。
- **改 full 返回 400**：Docker 部署主动禁用，使用 OpenSandbox 执行代码。

GitHub 的 `Docker smoke` workflow 构建并启动默认四个服务，检查新配置和旧开关兼容性，验证页面、登录和真实 OpenSandbox Python 执行。检查不调用真实模型或 SMTP。Windows 入口在 CI 中使用替身 Docker 检查配置、升级命令和协议路径校验，不会挂载 CI 磁盘或打开资源管理器；真实桌面协议打开仍需在部署者电脑上验证。


常见发件邮箱预设（QQ、163、企业邮箱、微软 OAuth）及连接/收信验证步骤见 [邮件服务配置](README-smtp.md)。
