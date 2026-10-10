# P11 本地磁盘读写 + OpenSandbox 执行（判断路由）

> 目标：**不需要执行代码时用本地磁盘读写文件；需要执行代码时进 OpenSandbox 沙箱执行，
> 执行结果（stdout/stderr + 产出文件）回给大模型。**
>
> 一句话实现：deepagents 的 `CompositeBackend` 里，**文件操作按路径路由、`execute` 不按
> 路径路由而永远走 `default`**。把 `default` 做成「本地磁盘为文件层 + 沙箱为执行层」的
> 混合后端，就同时拿到了两种语义，**不需要改 deepagents 一行代码**。

---

## 1. 三个决定性的框架事实（deepagents 0.7.1 实测）

| 事实 | 位置 | 意义 |
|------|------|------|
| `CompositeBackend` 的文件操作（ls/read/write/edit/delete/glob/grep）**按路径前缀路由**，未命中路由的路径走 `default` | `deepagents/backends/composite.py` | 把 `default` 指向本地磁盘，文件工具就落本地磁盘 |
| `CompositeBackend.execute()` **不按路径路由，永远委托给 `default`** | 同上，`execute()`/`aexecute()` | 把 `execute` 打到沙箱的唯一挂点 |
| `supports_execution(backend)`：`CompositeBackend` 时判断 `isinstance(backend.default, SandboxBackendProtocol)`，为真才**注册 execute 工具** | `deepagents/middleware/filesystem.py:1192` | default 必须**同时**是「文件后端」和「可执行后端」 |

于是组合形态是：

```
                        ┌──────────── CompositeBackend ────────────┐
模型的工具调用 ────────▶│ default = HybridDefaultBackend            │
                        │   ├─ 文件操作 → FilesystemBackend(root)   │──▶ 本地磁盘
                        │   │            （ls/read/write/edit/glob/grep/delete）
                        │   └─ execute() → OpenSandboxBackend       │──▶ OpenSandbox 容器
                        │                （+ 执行前后文件同步）      │
                        │ routes = {"/workspace/": 同一个 default}   │  ← 仅满足权限校验
                        └──────────────────────────────────────────┘
                                        │
                        执行结果：ExecuteResponse(output, exit_code)
                                        │
                        作为 ToolMessage 回给大模型（模型据此决定下一步）
```

`HybridDefaultBackend` 的继承顺序是关键：

```python
class HybridDefaultBackend(FilesystemBackend, SandboxBackendProtocol):
    ...
```

- `FilesystemBackend` 在前 → 文件方法**全部继承本地磁盘实现**；
- 同时是 `SandboxBackendProtocol` 实例 → `supports_execution()` 为真 → execute 工具被注册，
  且 `CompositeBackend.execute()` 会调用到本类的 `execute()`（内部转发给沙箱）。

---

## 2. 两条路径 + 一次同步（为什么要同步）

本地磁盘和容器是**两个独立文件系统**。如果不管，会出现「模型用 `write_file` 写了
`/workspace/data.csv`，`execute("python analyze.py")` 在容器里却找不到这个文件」。

所以 `HybridDefaultBackend.execute()` 的流程是四步：

```
1) push      本地新增/修改的文件 → 容器     （容器里的命令能读到模型刚写的文件）
2) snapshot  记录容器当前文件清单            （作为"这次执行产出了什么"的基线）
3) execute   真的在容器里跑命令              （stdout/stderr + exit_code → 回给大模型）
4) pull      容器里新增/修改的文件 → 本地磁盘（产出落地到本地，用户能直接看到/下载）
```

关键实现细节（都是踩过的坑）：

- **diff 只在沙箱侧做**：`pull` 用「执行前快照 vs 执行后清单」比较。**不要**拿容器 mtime
  和宿主 mtime 比 —— 两侧时钟/文件系统语义不同，会误判（把产出当旧文件不拉回）。
- **清单用 `find` 而不是 SDK 的 `search`**：`find <root> -type f -printf '%T@\t%s\t%p\n'`
  给 epoch 秒 + 字节数，一次遍历拿到整棵树，输出稳定；`files.search` 的 pattern 语义
  由服务端实现决定，容易漏掉嵌套文件。
- **只传增量的两边**：`push` 用本地 `{路径: (mtime_ns, size)}` 指纹，只传自上次以来变过的；
  `pull` 用容器清单 diff；每次结束后刷新两侧基线，避免来回倒腾。
- **路径折前缀**：模型看到的 `/workspace/a.txt` 在本地是 `<root>/a.txt`（前缀折掉），
  在容器里是 `/workspace/a.txt`（就是它本身）。见 §4。

---

## 3. OpenSandbox 后端（`OpenSandboxBackend`）

只实现 `SandboxBackendProtocol` 必需的四个方法，其余交给 `FilesystemBackend`（本地磁盘）：

```python
class OpenSandboxBackend(SandboxBackendProtocol):
    @property
    def id(self) -> str: ...
    def execute(self, command, *, timeout=None) -> ExecuteResponse: ...   # SDK commands.run
    def upload_files(self, files) -> list[FileUploadResponse]: ...        # SDK files.write_file
    def download_files(self, paths) -> list[FileDownloadResponse]: ...    # SDK files.read_bytes
```

- 用**同步 SDK**（`opensandbox.sync.SandboxSync`），异步工具入口用 `asyncio.to_thread`
  包一层，避免阻塞事件循环（LangChain 会优先调用 `aexecute`）。
- 沙箱**按会话复用**：`SandboxRegistry` 里 `session_key → 容器连接`；每个会话一个容器
  （会话间隔离），进程退出时 `atexit` 统一 `destroy()`；容器 TTL 到期后 `is_healthy()`
  为假会自动重建。
- 命令里的绝对路径会做一次翻译（`translate_command`）：`python /scripts/a.py` →
  `python /workspace/scripts/a.py`；`/usr`、`/bin` 等系统目录和已是容器路径的不动。

---

## 4. 路径前缀 `/workspace`（为什么模型不需要做路径翻译）

| 视角 | 同一个文件 |
|------|-----------|
| 模型看到的工具路径 | `/workspace/report.md` |
| 本地磁盘真实位置 | `<repo>/workspace/report.md` |
| 容器内真实位置 | `/workspace/report.md`（容器工作目录也是 `/workspace`） |

`FilesystemBackend(root_dir=<repo>/workspace, virtual_mode=True)` 默认把 `/report.md`
映射到 `<root>/report.md`，为此 `HybridDefaultBackend` 覆写了全部路径入口
（`_resolve_path` / `_display_path` / `ls` / `read` / `write` / `edit` / `delete` /
`glob` / `grep`），先做一次 `_strip_prefix()` 把 `/workspace` 折掉：

```python
def _strip_prefix(self, key):        # "/workspace/a.txt" → "/a.txt"
    ...
def _display_path(self, path):       # 反向：本地真实路径 → "/workspace/a.txt"
    ...                              # （否则会把宿主绝对路径泄露给模型）
```

同时 `glob` 的 **pattern** 也要折前缀（`/workspace/**/*.py` → `**/*.py`），
否则 `FilesystemBackend.glob` 会把它当"相对根目录"的路径而匹配不到任何文件。

---

## 5. 权限（HITL）与两个必须知道的约束

一旦 backend 具备执行能力，`FilesystemMiddleware.__init__` 会强制要求：
**`permissions` 里每条规则的每个路径都必须以某个 route 前缀开头**
（`_all_paths_scoped_to_routes`），否则直接抛 `NotImplementedError` ——
因为 `execute` 工具本身不吃路径级权限。

### 5.1 主 Agent：声明一条指向自身的占位 route

```python
backend.routes["/workspace/"] = backend.default      # 读写位置完全不变
```

它只让 `/workspace/**` 的写操作审批规则（`mode="interrupt"`，触发 deepagents 内置
HumanInTheLoopMiddleware）能通过校验。副作用仅是 `ls("/")` 会多出一条 `/workspace/`
目录条目。

### 5.2 子智能体：权限路径也必须 route 化（**踩过的坑**）

`create_deep_agent` 会为**每个子智能体**单独构造一个 `FilesystemMiddleware`，
用的是**父级的 backend**（`deepagents/graph.py:668`）：

```python
subagent_permissions = spec.get("permissions", permissions)   # 子代理没写就继承父级
subagent_middleware = [FilesystemMiddleware(backend=backend, _permissions=subagent_permissions), ...]
```

于是 §5 的校验对子智能体同样生效。典型故障：主 Agent 权限改好了，
但 `configs/subagents/*.yaml` 里写的是 `/notes/`，不在 `/workspace/` 前缀下 →
对话时抛

```
NotImplementedError: FilesystemMiddleware does not yet support permissions with backends
that provide command execution (SandboxBackendProtocol).
```

而且**只在存在子智能体时才暴露**，很容易误判成主 Agent 的问题。

修法：子智能体 YAML 里写**工作区内相对路径**，由 `build_subagent()` 统一补前缀 + `**`：

| 配置里写的 | 生成的权限路径 | 含义 |
|-----------|---------------|------|
| `/` | `/workspace/**` | 整个工作区 |
| `/notes/` | `/workspace/notes/**` | 工作区下的 notes 子目录 |
| `/workspace/a` | `/workspace/a/**` | 已带前缀，只补通配 |

另外 `runtime.get_agent()` 会调 `sandbox.validate_permissions()` 做**启动期自检**：
把主 Agent 与所有子智能体的权限路径一起核一遍，有问题就抛一条说得清楚的错误，
而不是把框架内部的 `NotImplementedError` 直接甩给用户。

---

## 6. 一次 execute 的完整时序

```
模型调用 execute(command="python analyze.py")
  │
  ▼ FilesystemMiddleware.execute  (deepagents，已注册：因为 default 可执行)
  ▼ CompositeBackend.execute      (不按路径路由 → 委托 default)
  ▼ HybridDefaultBackend.execute
  │
  ├─① push_local_changes()        本地自上次以来改动过的文件 → 容器（upload_files）
  ├─② list_files()                记录容器清单（执行前快照）
  ├─③ OpenSandboxBackend.execute  translate_command → SDK commands.run(cwd=/workspace)
  │                                 → ExecuteResponse(output, exit_code)
  ├─④ pull_sandbox_changes()      与快照 diff → 产出文件 download_files → 写回本地磁盘
  ▼
ExecuteResponse 被包装成 ToolMessage
  ▼
回给大模型（stdout/stderr + 退出码）→ 模型据此决定下一步
```

③ 的请求发往 `SANDBOX_DOMAIN`；若 `SANDBOX_USE_SERVER_PROXY=true`，
执行命令与传文件都经 OpenSandbox 服务端代理（远端部署见 §7）。

---

## 7. 远端部署：怎么连上 Linux 服务器里的 docker

**先明确一件事**：harness 不直接连 docker，你连的是 **OpenSandbox 服务端**，
由它去创建/管理容器。所以要解决的是两个独立问题：

```
Windows(harness) ──①──▶ OpenSandbox 服务端(Linux:8080) ──②──▶ 沙箱容器(execd)
                     你要解决的是①              服务端自己解决②（或需要 use_server_proxy）
```

② 是关键：SDK 默认会向服务端索要**容器直连地址**，然后自己去连那个地址。
容器在 docker 私有网段（172.17.0.x）时，Windows 上的 harness **连不到**。
`SANDBOX_USE_SERVER_PROXY=true` 让 ② 也走服务端代理，客户端就只需要能访问 ① 一个地址。

### 方案 A：SSH 隧道（推荐，不用改服务端）

远端服务只 bind 在 `127.0.0.1:8080` 时，从 Windows 建隧道把它"搬"到本地端口：

```powershell
# 保持这个窗口开着（后台跑也可加 -f -N）
ssh -N -L 18080:127.0.0.1:8080 <user>@<linux-ip>
```

```dotenv
# backend/.env
SANDBOX_DOMAIN=127.0.0.1:18080      # 本地端口 → 隧道 → 服务器 127.0.0.1:8080
SANDBOX_USE_SERVER_PROXY=true       # 容器也不直连，全部走服务端
```

隧道相当于"你就在服务器本机"，所以 ② 里的容器地址（127.0.0.1:xxxx 或 docker 网关）
在代理模式下由服务端自己解析，不需要你操心。**这也是 Windows 上最省事的组合**。

### 方案 B：服务端监听对外地址

```bash
# Linux 服务器上（docker-compose 或启动参数）
#   把服务端口绑定到 0.0.0.0:8080，并确保防火墙放行
sudo ufw allow 8080/tcp        # 示例
```

```dotenv
SANDBOX_DOMAIN=<linux-ip>:8080
SANDBOX_USE_SERVER_PROXY=true
```

安全前提：`SANDBOX_API_KEY` 必须配好（SDK 用 `OPEN-SANDBOX-API-KEY` 头认证），
否则等于把一个能起容器的接口裸奔在公网上；建议只在内网/白名单开放。

### 方案 C：容器端口已映射出来

如果服务端把容器 execd 端口映射到了宿主机，且 Windows 能访问那些端口，
才可以 `SANDBOX_USE_SERVER_PROXY=false`（直连容器）。跨机场景一般不成立。

### 关于 Windows 的一个坑

SDK 会通过 `getifaddrs(3)` 探测**客户端自己的内网 IP**，并用
`OPEN-SANDBOX-CLIENT-IP` 头告诉服务端（服务端据此判断该给客户端返回哪个容器地址）。
**Windows 没有 `getifaddrs`，这个头会是空**。若服务端依赖它做地址选择，可手填：

```dotenv
SANDBOX_HEADERS={"OPEN-SANDBOX-CLIENT-IP": "192.168.1.50"}
```

### 自检脚本

```powershell
cd D:\Deepseek_Harness\agent-harness\backend

# 只探测服务端可达（TCP + HTTP）
python ../_probe/sandbox_connect_check.py

# 完整验证：建容器 → 跑命令 → 传/读文件 → 销毁
python ../_probe/sandbox_connect_check.py --full

# 覆盖配置试不同组合
python ../_probe/sandbox_connect_check.py --full --domain 127.0.0.1:18080 --proxy
python ../_probe/sandbox_connect_check.py --full --domain 192.168.1.20:8080 --no-proxy

# 最终验收：走 harness 生产代码路径（本地磁盘 + 远端沙箱 + 产出回写）
python ../_probe/verify_remote_sandbox.py
```

脚本按「TCP → HTTP → 建容器 → 执行命令 → 文件读写」逐级报错，哪一步断了一眼就能看出。

### 实际部署记录（已跑通）

| 项 | 值 |
|----|----|
| 服务器 | 阿里云 ECS，公网 `39.105.158.228`，Ubuntu，Docker 29.8.0 |
| 服务端 | `opensandbox-server` 0.2.3，装于 `/root/.venv`，配置 `/root/sandbox.toml` |
| 服务端监听 | `127.0.0.1:8080`（仅本机），`[docker] network_mode="bridge"`，host 端口段 40000–60000 |
| API key | 在 `/root/sandbox.toml` 的 `[server].api_key`；本机放 `backend/.env` 的 `SANDBOX_API_KEY`（`.env` 已 gitignore） |
| 隧道 | 本机 `127.0.0.1:18080` → 服务器 `127.0.0.1:8080`，脚本 `_probe/sandbox_tunnel.ps1` |
| 客户端 | `SANDBOX_DOMAIN=127.0.0.1:18080` + `SANDBOX_USE_SERVER_PROXY=true` |

**踩过的三个坑（都在服务器侧）**：

1. **`uv pip install opensandbox-server` 卡死**：进程停在连接 `151.101.128.223:443`（国外 PyPI），
   uv 缓存只到 27M。加 `--index-url https://mirrors.aliyun.com/pypi/simple/` 后 **45 秒装完**。
2. **docker 拉不到镜像**：`/etc/docker/daemon.json` 里 20 多个镜像源绝大多数已失效 ——
   `mirror.aliyuncs.com` DNS 解析失败、`docker.registry.cyou` 的 blob 下载 TCP 超时、
   `dockerproxy.com` 无响应、jsdelivr 系返回 429。实测可用：`docker.1panel.live`、
   `docker.m.daocloud.io`。已改为 5 个可用源（原文件备份为 `daemon.json.bak.*`）。
3. **创建容器 30 秒超时**：SDK 默认 `request_timeout=30s`，而首次创建要拉
   `python:3.11` + `opensandbox/execd:v1.0.22` 两个镜像。已新增
   `SANDBOX_CREATE_TIMEOUT_SECONDS`（默认 300）。

**验证结果**：容器创建 ✓ → 执行命令返回 `sum= 42`（读到了本机推送进容器的文件）✓
→ 容器产出 `result.txt` 自动回写本地磁盘 ✓ → 容器销毁 ✓。

---

## 8. 配置（`app/core/config.py` / `.env`）

| 变量 | 默认 | 说明 |
|------|------|------|
| `SANDBOX_ENABLED` | `true` | execute 是否进沙箱；`false` 时文件工具照常可用本地磁盘，execute 返回可读错误 |
| `SANDBOX_SYNC_ENABLED` | `true` | 是否做执行前后的本地↔沙箱文件同步 |
| `SANDBOX_DOMAIN` / `SANDBOX_PROTOCOL` | `127.0.0.1:8080` / `http` | **客户端能访问到**的 OpenSandbox 服务地址（远端部署见 §7） |
| `SANDBOX_USE_SERVER_PROXY` | `true` | 执行命令/传文件是否经服务端代理；容器 IP 客户端不可达时必须 true |
| `SANDBOX_API_KEY` | 空 | 服务端要求时填（`OPEN-SANDBOX-API-KEY` 头） |
| `SANDBOX_HEADERS` | 空 | 额外请求头 JSON（如手填 `OPEN-SANDBOX-CLIENT-IP`） |
| `SANDBOX_IMAGE` | `python:3.11` | 容器镜像（需自带 python3 + coreutils） |
| `SANDBOX_ROOT` | `/workspace` | 容器内工作目录 = 模型看到的路径前缀 |
| `SANDBOX_CREATE_TIMEOUT_SECONDS` | `300` | 创建沙箱的 HTTP 超时（首次拉镜像需要） |
| `SANDBOX_TTL_MINUTES` | `60` | 容器最长存活，到期服务端回收（本地磁盘文件不受影响） |
| `SANDBOX_MOUNT_MACHINE_DIRS` | `false` | 把 `/mnt/<名>` 挂载点**只读挂进容器**（方案 A，见 §11）；需服务端同机 + 运行时支持 host mount |
| `SANDBOX_MOUNT_READ_ONLY` | `true` | 挂载是否只读。保持 `true` 才能让「写本机文件」继续走宿主 FS + 人工审批 |
| `HARNESS_WORKSPACE` | `<repo>/workspace` | 本地磁盘读写的根目录 |

---

## 9. 代码位置与验证

| 文件 | 职责 |
|------|------|
| `backend/app/agent/opensandbox_backend.py` | `PathMapper` / `translate_command` / `SandboxRegistry` / `OpenSandboxBackend` / `HybridDefaultBackend` / `build_hybrid_backend` |
| `backend/app/agent/sandbox.py` | 对外组装：`build_main_backend()`（含权限 route）、`default_permissions()`、`validate_permissions()`（启动期自检）、`execute_allowed()`、`sandbox_status()` |
| `backend/app/agent/subagents.py` | 子智能体权限路径的 route 前缀归一化（`_to_route_path`） |
| `backend/app/agent/runtime.py` | 按会话缓存 agent：`get_agent(session_key)` / `get_main_backend(session_key)` |
| `backend/tests/test_p11_hybrid_backend.py` | 假容器下的路由 + 同步闭环测试（不联网、不建真容器） |
| `backend/tests/test_p6_subagents.py` | 子智能体权限 route 化回归测试 |
| `_probe/hybrid_backend_probe.py` | 独立探针脚本（pytest conftest 会 `os._exit` 吃掉失败报告，故单独跑） |
| `_probe/sandbox_connect_check.py` | 远端 OpenSandbox 连通性自检（TCP → HTTP → 建容器 → 执行 → 文件读写） |
| `_probe/repro_get_agent.py` / `_probe/repro_sse.py` | 复现「子智能体权限未 route 化」的启动期/SSE 验证脚本 |

实测结论（`create_deep_agent` 真实构造，不调用模型）：

```
registered tools: ['delete', 'edit_file', 'execute', 'glob', 'grep',
                   'ls', 'read_file', 'task', 'write_file']
```

`execute` 与文件工具同时在场 —— 说明「本地磁盘读写 + 沙箱执行」的路由已生效。

---

## 10. 已知边界

- 沙箱不可用（服务没起 / `SANDBOX_ENABLED=false`）时，`execute` 返回错误文本而不是抛异常，
  整轮对话不中断；文件工具不受影响。
- 同步有体积上限（`SYNC_MAX_BYTES = 32MB`，忽略 `.git`/`node_modules`/`__pycache__` 等）。
  大文件场景改用**宿主机目录挂载**（`SANDBOX_MOUNT_MACHINE_DIRS`，见 §11），零拷贝、无上限。
- 同步是**每个 execute 前后各一次**，不是实时双向挂载：如果模型用 `execute` 删除了容器
  文件，本地磁盘上的对应文件不会被删（pull 只写不删，避免误删用户数据）。
- `routes` 里的其它虚拟路径（如未来的 `/memories/` → `StoreBackend`）**不在沙箱可见范围**，
  容器里的命令读不到它们。
- ⚠️ **`execute` 的失败永远不会触发熔断**（deepagents 源码事实）：`FilesystemMiddleware.
  _format_execute_output()` 只把输出拼上 `[Command failed with exit code N]`，**既不设
  `status="error"` 也不加 `Error:` 前缀**（只有 `NotImplementedError` 才返回 `Error: Execution
  not available.`）。而熔断器 `middleware._is_tool_failure()` 只认 `status=="error"` /
  `Error:` 开头 ⇒ **命令级失败被判定为"成功"、不计数**。后果：模型反复换招且永不熔断
  （实测一次任务里 `execute` 20 次 + MCP 19 次 + `read_file` 12 次）。
  这是 §11 要解决的核心故障；未启用 §11 时，建议自行在 `HybridDefaultBackend.execute()`
  里把「引用了 `/mnt/` 且失败」的输出改写为 `Error:` 开头，让熔断器能兜住。
- `SANDBOX_VOLUMES`（任意 host→container 映射的显式配置）尚未提供：当前只支持「把
  `HARNESS_MACHINE_MOUNTS` 的挂载点按同一虚拟路径挂进容器」。需要挂一个**不在** `/mnt`
  命名空间下的目录时，再加这个配置。

---

## 11. 方案 A：把本机挂载点只读挂进容器（`SANDBOX_MOUNT_MACHINE_DIRS`）

### 11.1 要解决的故障

P12 把本机磁盘暴露成 `/mnt/<名>/...`，但那只对**文件工具**有效 —— 容器里没有 `/mnt`：

```
read_file  /mnt/d/docs/budget.xlsx          → ✅ 成功（走宿主 FilesystemBackend）
execute("python -c \"import pandas; pandas.read_excel('/mnt/d/docs/budget.xlsx')\"")
                                            → ❌ 容器里没有这个路径
```

而 `execute` 的失败**不会触发熔断**（见 §10 最后一条）⇒ 模型不断换招：换工具、换路径写法、
换命令，直到把每条路都试完。实测一次名称比对任务里出现 **`execute` 20 次 +
`get_workbook_metadata` 19 次 + `read_file` 12 次 = 51 次空转**。

根因不是模型笨，而是**同一个路径字符串在三个执行器里语义不同**：

| 模型看到的路径 | 文件工具（宿主 FS） | `execute`（容器） | MCP 工具（宿主进程） |
|---|---|---|---|
| `/workspace/...` | ✅ | ✅（同步后） | ❌ 不认虚拟路径 |
| `/mnt/d/...` | ✅ | ❌（**本节要修的就是这格**） | ❌ 需要 `D:\...` |

### 11.2 做法

把每个挂载点以**只读**方式挂进容器，**容器内路径沿用模型侧的 `/mnt/<名>`**：

```python
volumes=[Volume(name="machine-d", host=Host(path="D:/docs"),
                mount_path="/mnt/d", read_only=True)]
```

于是 `read_file /mnt/d/a.xlsx` 与 `execute("... '/mnt/d/a.xlsx' ...")` 指向
**同一个文件、同一个路径字符串** —— 表格里那一格从 ❌ 变成 ✅，路径命名空间不一致从根上消失。

顺带三个收益：**零拷贝**（大文件不再受 32MB 同步上限约束）、**不会出现「两份真相」**
（挂载是同一份文件，不像同步那样复制）、**只读挂载天然安全**（写本机文件仍只能走宿主 FS + 审批）。

### 11.3 关键实现点（都踩过或差点踩）

1. **容器内路径必须与模型侧一致**（`sandbox_mount_specs()`）。挂到别的路径（比如
   `/mnt/host/d`）等于把刚解决的问题换个地方重现。
2. **`volumes` 是创建期参数 ⇒ 必须进沙箱缓存签名**（`sandbox_mount_key()` +
   `_SandboxEntry.volumes_key`）。沙箱按会话缓存，不进签名就会出现「配置改了但行为没变」——
   模型仍旧在容器里找不到 `/mnt`，而你以为已经修好了。
3. **创建失败必须降级 + 大声报错**：SDK 注明 host mount「仅当运行时支持时可用」。
   带 volumes 创建失败时退回无挂载容器，并记 `ERROR`（说明"容器内看不到 /mnt"），
   **绝不静默假装成功**。降级后 `volumes_key` 记为空串 ⇒ 下次仍会尝试带挂载创建。
4. **只读**（`SANDBOX_MOUNT_READ_ONLY=true`）。改成可写会让容器**绕过 HITL 审批**
   直接写本机文件 —— 与 P12 的「挂载点写操作需审批」冲突。
5. **同步范围不变**：仍只覆盖 `/workspace`。挂载点是只读镜像，不该被 push/pull 触碰。

### 11.4 限制（务必先确认）

| 限制 | 说明 |
|---|---|
| **必须同机** | `Host.path` 是**沙箱服务端所在主机**的路径。远端部署（如阿里云 ECS）上写 `D:/` 毫无意义 —— 本节只适用于本机部署。 |
| **运行时必须支持 host mount** | Docker/K8s 需允许 hostPath；不支持则创建失败并降级（见 11.3-3）。 |
| **不替代同步** | 挂载是只读的；模型要**产出**文件仍走 `/workspace` + 同步回本地。 |
| **写回本机仍受限** | 想让模型改写本机文件，只能走文件工具的 `/mnt/...` 路径（受审批保护），不能走 `execute`。 |

### 11.5 配置与验证

```bash
# backend/.env
HARNESS_MACHINE_ACCESS=true
HARNESS_MACHINE_MOUNTS=d=D:/,proj=D:/work/api   # 留空 = 自动挂载本机所有盘符
SANDBOX_MOUNT_MACHINE_DIRS=true
SANDBOX_MOUNT_READ_ONLY=true
```

```bash
# 先看配置算出来的挂载规格（不建容器、不联网）
C:\Users\Administrator\anaconda3\envs\agent_env\python.exe -c ^
  "import os,sys; os.chdir('backend'); sys.path.insert(0,'backend'); ^
   from app.agent.opensandbox_backend import sandbox_status; ^
   import json; print(json.dumps(sandbox_status()['sandbox_volumes'], ensure_ascii=False))"

# 再走真容器验证「容器内能不能看到 /mnt」
C:\Users\Administrator\anaconda3\envs\agent_env\python.exe _probe\verify_sandbox_mount.py
```

`/api/v1/*` 的沙箱状态摘要里也能看到 `mount_into_sandbox` / `mount_read_only` /
`sandbox_volumes` —— 排查「挂载没生效」时先看这里。

单测覆盖（`backend/tests/test_p12_machine_mounts.py`，不联网、不建真容器）：
规格计算与关闭时为空、容器路径与 `machine_mount_prefix` 对齐、指纹随配置变化、
SDK `Volume` 字段映射、**配置变更强制重建容器**、状态摘要暴露、同步范围不受影响。
