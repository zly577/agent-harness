# 稳定性与性能优化说明

本次优化保持现有界面和单进程部署方式，主要处理并发请求、任务取消、流式连接和大输出场景。无需迁移业务数据。

## 1. 登录切换时隔离旧请求

旧登录发出的请求可能在退出、重新登录后才返回。此前旧请求的 401 会清除新登录的凭证，旧成功响应也可能覆盖新用户的界面。

现在每次登录身份变化都有一个递增版本。API 在收到响应及读取响应正文后检查版本，SSE 在派发事件前也检查；过期请求抛出 `StaleSessionError`，不再修改当前凭证。界面的会话、供应商、权限配置加载同样检查版本。退出时清理会话缓存、附件、文件面板和 Markdown 缓存。

涉及：`frontend/src/api.js`、`frontend/src/sse.js`、`frontend/src/App.vue`。

## 2. SQLite 初始化串行化

原来的延迟初始化在并发首次访问时，可能建立多个连接并重复创建 saver/store。

现在连接、checkpointer、store 的初始化及关闭共用异步锁，只发布已经完成 setup 的实例；初始化失败后允许重试。连接建立过程中收到取消，也会等待连接返回并关闭尚未发布的连接，避免遗留后台线程。

涉及：`backend/app/agent/context.py`。

## 3. 区分“请求停止”和“已经停止”

此前停止等待超时仍可能返回成功，导致界面显示完成，或会话已经删除而任务仍在写数据。

现在运行状态包含 `stopping`。重复点击停止不会再次取消正在清理的协程。超过等待时间但任务仍活着时，停止接口返回 HTTP 202 和 `status: stopping`；前端保留运行状态并提示等待。此时删除会话或工作区返回 HTTP 409，避免提前清除运行依赖。正常停止、已经空闲的响应保持原有行为。

服务关闭时等待生产任务退出；超过关闭期限会记录错误并使生命周期关闭报错，保留仍被使用的存储连接。遇到这种情况应检查未退出的工具或命令。

涉及：`backend/app/agent/runner.py`、`backend/app/api/routes/sessions.py`、`backend/app/main.py`。

## 4. Shell 输出在采集时限量

此前使用 `communicate()` 收集全部输出后才截断，大量日志仍可能先占满内存。

现在 stdout/stderr 各有独立读取线程，持续排空管道，每路最多保留 `max_output_bytes`，默认 100,000 字节。超过部分丢弃，避免命令因管道塞满卡住；返回内容仍按原有总字节上限截断，并标记 `truncated`。超时和取消时保留已采集的部分输出及对应退出码。

这不是完整日志归档功能。需要完整输出时应让命令写入工作区文件，再按需读取。显式脱离进程树的后台程序仍不保证随父进程退出，命令应以前台方式运行。

涉及：`backend/app/agent/host_shell.py`。

## 5. 审批和重接流绑定具体运行

审批恢复、问题回答以及刷新重接接口都捕获具体的 `SessionRun` 并显式传给订阅方法。即使同一会话随后启动另一轮，旧连接也不会误订阅新一轮的数据。

涉及：`backend/app/api/routes/approvals.py`、`backend/app/api/routes/sessions.py`。

## 6. 文件操作移出事件循环，下载按块发送

文件列表、后端定位、删除等同步操作移到线程池，慢磁盘不会阻塞整个异步事件循环。

本地文件下载沿用原后端的路由和路径检查，打开后按 64 KiB 分块读取；正常结束或下载中断后关闭文件。下载限定于普通文件，保留目录穿越防护；文件增长时只发送开始下载时的长度。非本地文件后端继续使用其原有下载接口，是否一次加载全文件由该后端决定。

涉及：`backend/app/api/routes/files.py`、`backend/app/services/files.py`。

## 7. SSE 心跳和异常断线提示

聊天、审批恢复、问题回答和运行重接使用统一的 SSE 输出方法。没有业务事件时每 15 秒发送注释心跳；心跳不会取消等待中的事件，也不会进入聊天内容或运行回放。

前端只有收到显式 `event: done` 才认为流正常结束。提前 EOF 或读取失败会保留已经显示的内容，并提示“后台任务可能仍在运行，刷新页面可重新接回”。关闭订阅不会停止后台任务。

本次没有新增自动重连或请求重试，仍通过刷新/重新打开会话接回运行，避免重复发送消息或审批决定。

涉及：`backend/app/api/routes/chat_stream.py`、`frontend/src/sse.js`、`frontend/src/App.vue`。

## 8. 限制前端历史扫描与渲染缓存

滚动跟随从深度监听全部消息，改为观察最后一条消息的长度、状态、工具及子任务进度，避免每次流式增量遍历整段历史和大型工具参数。

Markdown 缓存改为 LRU，同时限制 128 项及约 2 MiB 的字符串开销。达到上限时逐项淘汰最久未使用内容，单个过大条目不缓存，退出登录时清空。此预算是 JavaScript 字符串估算，不代表浏览器总内存上限。

涉及：`frontend/src/composables/useMessageScroll.js`、`frontend/src/markdown.js`、`frontend/src/renderCache.js`。

## 9. 固定发布依赖

`backend/requirements.txt` 保留直接依赖及兼容范围；新增 `backend/requirements-lock.txt` 固定直接与传递依赖。Docker 和 `requirements-dev.txt` 使用锁文件，减少新装环境随时间漂移。

当前锁面向 Python 3.12 的 Windows 和 Linux，平台专属包用环境标记区分。Windows 版本以本次通过回归的环境为约束，Linux 特有依赖由解析器求解。此文件不承诺其他操作系统或 Python 版本。

常规安装，在仓库根目录执行：

```powershell
python -m pip install -r backend/requirements-dev.txt
```

更新依赖时：

1. 在独立 Python 3.12 环境更新 `backend/requirements.txt` 中需要调整的范围，安装并验证预期版本，执行 `python -m pip check`。
2. 安装独立的锁文件生成工具 `uv`，使用已验证环境的 Python 执行下列脚本。脚本只解析并生成文件，不安装或升级当前环境中的包。已安装包以实际生效的版本作为约束；当前平台未安装的包由解析器求解。
3. 审阅锁文件差异，在干净环境安装新锁，执行后端测试、前端测试与构建，并验证 Linux Docker 构建后再发布。清单与锁文件一并提交。

```powershell
python backend/scripts/lock-dependencies.py
python -m pip check
cd backend
python -m pytest -o addopts='' -q
```

脚本支持 `--output` 指定预览文件。它不会读取 API Key 或应用 `.env`。

## 10. Docker 默认提供完整执行环境

`docker-compose.yml` 现在默认包含前端、后端、MySQL 和 OpenSandbox，后端等待数据库与沙箱服务健康后启动。Docker 固定启用内置沙箱并使用 `opensandbox:8080`，旧 `.env` 中的 `SANDBOX_ENABLED=false` 或宿主机地址不再覆盖默认值。生成的新配置与说明文档同步更新；旧沙箱叠加文件和 Windows `-Sandbox` 参数保留兼容，普通命令即可启动完整服务。

`deploy/smoke-test.py` 增加真实执行验收：由后端 SDK 创建临时沙箱，通过服务代理执行 Python，验证输出后销毁容器。普通部署检查 full 禁用策略，Windows 个人部署使用 `--personal` 检查其权限策略。`SMOKE_BASE_URL` 和 `SMOKE_ENV_FILE` 可用于独立验证项目。

GitHub Docker workflow 使用默认命令启动四个服务，并预拉执行镜像、运行真实沙箱检查。配置回归覆盖首次安装、旧关闭开关、旧叠加命令和已有密钥保留；Windows 启动检查覆盖无参数默认启动与旧参数兼容。后端 CI 同时覆盖 Windows 与 Linux，Linux 的 LocalShell 提示词与实际 `/bin/sh` 描述保持一致。

## 验证范围

新增回归覆盖登录切换竞争、SSE 非正常结束、并发存储初始化、连接初始化取消、停止超时、关闭顺序、心跳、断线退订、管道输出限量、文件下载中断和渲染缓存预算。原生 Shell 测试同时覆盖 Windows PowerShell 和 PowerShell 7；审批/问题回答测试检查订阅绑定本次运行。

2026-10-26 验证结果：

| 检查 | 结果 |
|---|---|
| 独立 Windows Python 3.12 环境，按锁文件重新安装 | 成功，依赖兼容检查通过，应用与 `pywintypes` 导入成功 |
| 独立环境完整后端回归 | 530 passed、4 skipped，另有 12 个 subtests 通过 |
| Linux Python 3.12 容器完整后端回归 | 508 passed、26 skipped，另有 12 个 subtests 通过；Windows 原生 shell 测试按平台跳过 |
| 前端 `npm test` | 20 passed |
| 前端 `npm run build` | 成功；仍有产物超过 500 kB 的体积提示 |
| 锁文件生成脚本 | 重新生成的 103 条版本记录与提交候选文件一致 |
| `git diff --check` | 通过 |
| Docker 默认配置与旧配置兼容检查 | 3 项通过；Windows 启动与原生目录选择脚本检查通过 |
| Docker 容器构建与启动 | 四个服务健康；Windows 个人入口无需 `-Sandbox`，页面、登录、权限和真实 OpenSandbox Python 执行通过 |
| 全新隔离 Docker 部署 | 独立随机配置与空数据卷，默认 Compose 启动四个服务；页面、登录、权限和沙箱 Python 执行通过，随后清理本次测试资源 |

本次尚未增加历史分页、列表虚拟滚动、自动重连或多实例共享状态。后端仍按单实例、单 worker 运行。真实模型服务与 SMTP 不在本次联调范围内；OpenSandbox 另有实际容器执行验证，不依赖离线回归推断。
