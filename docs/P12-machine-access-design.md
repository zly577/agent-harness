# P12 本机磁盘访问（默认环境 = 本机电脑，只有 execute 进沙箱）

> 目标：**智能体的默认工作环境就是用户这台电脑** —— 文件工具直接读写本机真实文件；
> **只有跑代码（`execute`）才进沙箱容器**。
>
> 一句话实现：在 P11 的 `CompositeBackend` 上，把本机磁盘按盘符挂成**额外的 route**
> （`/mnt/<盘符>/...` → `FilesystemBackend(root_dir="D:/")`）。因为 `execute` 不按路径路由、
> 永远走 `default`（P11 已确立），所以新增 route **只影响文件工具，不改变 execute 的去向**。

---

## 1. 最终形态

| 模型看到的路径 | 真实落地位置 | 会进沙箱容器吗 |
|---------------|-------------|---------------|
| `/workspace/report.md` | `<repo>/workspace/report.md` | ✅ 执行前后自动同步（P11） |
| `/mnt/d/Deepseek_Harness/a.py` | `D:\Deepseek_Harness\a.py` | ❌ 只读写本机磁盘 |
| `/mnt/home/notes/x.md` | `C:\Users\Administrator\notes\x.md` | ❌ 只读写本机磁盘 |
| `execute("python x.py")` | OpenSandbox 容器（cwd=`/workspace`） | — 这就是"沙箱"本身 |

```
                         ┌──────────────── CompositeBackend ────────────────┐
模型的工具调用 ─────────▶│ default = HybridDefaultBackend                    │
                         │   ├─ 文件 → FilesystemBackend(<repo>/workspace)   │──▶ 工作区（+沙箱镜像）
                         │   └─ execute → OpenSandboxBackend                 │──▶ 容器
                         │ routes = {                                        │
                         │   "/workspace/" → default   （权限校验占位，P11）  │
                         │   "/mnt/d/"     → MountedDiskBackend(D:/)         │──▶ 本机 D 盘
                         │   "/mnt/c/"     → MountedDiskBackend(C:/)         │──▶ 本机 C 盘
                         │   "/mnt/"       → MachineRootsBackend（只回答 ls） │
                         │ }                                                 │
                         └───────────────────────────────────────────────────┘
```

---

## 2. 为什么这套加法是安全的（框架事实）

| 事实 | 位置 | 意义 |
|------|------|------|
| `CompositeBackend` 的文件操作按**路径前缀**路由，未命中走 `default` | `deepagents/backends/composite.py` | 新增 route 只接管 `/mnt/**`，工作区语义不变 |
| `execute()` **不按路径路由，永远委托 `default`** | 同上 | 加多少 route 都不会让 `execute` 跑到别的地方 |
| route 命中后**无条件剥前缀**（`suffix = path[len(prefix):]`） | 同上 `_route_for_path` | `/mnt/d/proj/a.py` → 交给 root=`D:/` 的后端时变成 `/proj/a.py` |
| `supports_execution()` 只看 `default` 是否实现 `SandboxBackendProtocol` | `middleware/filesystem.py` | 挂载 route 是普通文件后端，不影响 execute 注册 |
| 权限路径**必须**落在某个 route 前缀内 | `_all_paths_scoped_to_routes` | 每个挂载点都得是合法 route 前缀（见 §6） |

每个挂载点仍用 `FilesystemBackend(root_dir=<盘符或目录>, virtual_mode=True)`，**即仍然关在该盘内**：
`..`、越界绝对路径一样被拒（`ValueError: Path traversal not allowed`）。

---

## 3. 配置（`app/core/config.py` / `backend/.env`）

| 变量 | 默认 | 说明 |
|------|------|------|
| `HARNESS_MACHINE_ACCESS` | `false` | `true` = 文件工具可读写本机真实文件（挂成 `/mnt/<名>/...`）；`false` = 只有 `/workspace`（P11 行为） |
| `HARNESS_MACHINE_MOUNTS` | 空 | 逗号分隔的 `名称=路径` 或裸路径；**留空 = 自动挂载本机所有存在的盘符**（`c=C:/`、`d=D:/` …） |

```dotenv
# 整个本机（自动挂盘符）
HARNESS_MACHINE_ACCESS=true
HARNESS_MACHINE_MOUNTS=

# 只要几个目录（更收敛，推荐生产环境这样写）
HARNESS_MACHINE_ACCESS=true
HARNESS_MACHINE_MOUNTS=proj=D:/Deepseek_Harness,home=C:/Users/Administrator
```

挂载名会做安全归一（只留字母数字下划线、小写），重名自动加序号；不存在的路径直接跳过并告警
（配置写错不会炸掉建 Agent）。

---

## 4. `/mnt` 这一层为什么需要占位后端

`CompositeBackend` 只在两种情况聚合 route：**路径等于 `/`**，或**命中某个 route 前缀**。
`/mnt` 本身既不是 `/` 也不命中 `/mnt/d/`，于是 `ls /mnt` 会落到 `default`（工作区）——
模型会看到一堆与盘符无关的工作区文件，属于"静默给错答案"。

所以加了一条 `/mnt/` route，指向 `MachineRootsBackend`：**只回答 `ls`**（列出已挂载的
`c/d/e`），其余操作返回可读错误：

```
/mnt 只是本机磁盘的挂载目录，请使用具体挂载点：/mnt/c/, /mnt/d/, /mnt/e/
```

（`/mnt/nope/x.txt` 这类不存在的挂载点也走这里 → 得到同一句提示，而不是静默写到别处。）

---

## 5. 踩过的坑：`glob` 的模式是**相对 search path** 解释的

这是本次改动里唯一一个"看起来该工作却返回空列表"的地方，实测（`_probe/p12_glob_probe.py`）：

| 调用 | 结果 |
|------|------|
| `glob('proj/*.py')`（path=None） | ✅ 匹配（模式相对根） |
| `glob('*.py', path='/proj')` | ✅ 匹配 |
| `glob('/proj/*.py', path='/proj')` | ❌ **空**（绝对模式拿去和相对路径比对） |
| `glob('/proj/*.py')`（path=None） | ✅ 匹配 |

而 `CompositeBackend` 只有 `path=None`/`"/"` 那条分支会调用 `_strip_route_from_pattern`
把 route 前缀从模式里去掉；`path` 给定时**模式原样透传给 route 后端**。于是

```python
glob(pattern="/mnt/d/proj/*.py", path="/mnt/d/proj")   # 模型最自然的写法 → 空结果
```

修法：新增 `anchored_glob_args(pattern, path)`（`opensandbox_backend.py`），把绝对模式按
search path 折成相对模式（模式在 search path 之外时退化成"以根为锚"，即 path=None 的语义），
`MountedDiskBackend.glob` 与 `HybridDefaultBackend.glob` 共用它。

顺带修掉了 P11 里同类的隐藏问题：`glob("/workspace/sub/*.txt", "/workspace/sub")` 之前也是空结果
（P11 的测试只覆盖了工作区根目录，所以一直没暴露）。

---

## 6. 权限（HITL）

`default_permissions()` 现在为**每个挂载点**加一条写规则，与工作区同等待遇：

```python
FilesystemPermission(operations=["write"], paths=["/workspace/**"], mode="interrupt")
FilesystemPermission(operations=["write"], paths=["/mnt/d/**"],   mode="interrupt")
```

- 只管 `write`：`ls`/`read_file`/`glob`/`grep` **不会**触发审批（读本机文件不打断对话）；
- 写/改/删本机真实文件 → 弹审批窗（这是"直接改用户电脑上的东西"该有的默认门槛）；
- 不想要这层门槛：`HITL_ENABLED=false`（重启生效）或 `PUT /api/v1/hitl/config`（热切换）。

子智能体侧：`configs/subagents/*.yaml` 里的权限路径现在**可以直接写 `/mnt/d/proj`**
（`subagents._to_route_path()` 会识别已有的 route 前缀，不再叠 `/workspace`）。
注意它只在 `HARNESS_MACHINE_ACCESS=true` 时合法，否则启动期 `validate_permissions()` 会报出来。

---

## 7. 沙箱边界：`execute` 只镜像 `/workspace`（设计选择，不是缺陷）

本项目的沙箱部署在远端 Linux（阿里云 ECS，见 P11 §7），**没有**任何办法把本机 `D:\` 挂进
远端容器（bind mount 只对同机 docker 成立）。所以边界是：

- 文件工具：想读改本机哪个盘都行（`/mnt/...`）；
- `execute`：容器里只有 `/workspace`（= 本机 `<repo>/workspace` 的镜像）。

**正确的工作流**（提示词里已写死这条分工，模型会自动照做）：

1. 读/改用户电脑上的真实项目 → `/mnt/...` 路径；
2. 需要跑代码/装依赖/做数据分析 → 把脚本与数据写到 `/workspace/` 下，再 `execute`；
3. 交付物（报告、结果文件）落在 `/workspace/`，沙箱到期回收也不丢，用户还能一键下载。

另一个必须提醒模型的点：`glob`/`grep` **不带 `path`** 时会"搜所有后端"，也就是把所有挂载点
（整块盘）一起搜。框架自带 deadline（glob 每后端 5s、grep 15s，超时返回截断结果），不会挂死，
但很浪费——提示词里要求必须带 `path`。

以后若想让沙箱直接看到本机目录，可选（未实现）：本机 docker 部署 OpenSandbox + volumes 挂载，
或者做一个"把指定 `/mnt/...` 文件 stage 进容器"的工具。

---

## 8. 代码位置

| 文件 | 职责 |
|------|------|
| `backend/app/core/config.py` | `harness_machine_access` / `harness_machine_mounts` |
| `backend/app/agent/opensandbox_backend.py` | `machine_mounts()`（配置解析 + 自动挂盘符）、`machine_mount_prefix()`、`MountedDiskBackend`（折前缀 + glob 归一）、`MachineRootsBackend`（`/mnt` 占位）、`build_machine_routes()`、`anchored_glob_args()`、`sandbox_status()`（多了 `machine_access`/`machine_mounts`） |
| `backend/app/agent/sandbox.py` | `build_main_backend()` 接入挂载 route；`default_permissions()` 为每个挂载点加审批规则 |
| `backend/app/agent/subagents.py` | `_route_prefixes()` / `_to_route_path()` 识别 `/mnt/...` |
| `backend/app/agent/prompt.py` | `machine_mount_prompt_section()`：把**实际挂载点**写进系统提示词 + 沙箱边界说明 |
| `backend/tests/test_p12_machine_mounts.py` | 14 条用例（挂载路由、真实文件读写、越界、权限、子智能体、execute 仍进沙箱） |
| `_probe/p12_machine_probe.py` | 端到端验证（真实 `.env` + 真实建 Agent） |
| `_probe/p12_live_chat_probe.py` | **实机对话验证**：真跑一轮（自动同意审批），看模型是否用 `/mnt/...` 读本机文件、写到 `/workspace` 再 `execute` |
| `_probe/p12_glob_probe.py` | glob 语义探针（§5 的实测来源） |
| `backend/tests/_pytest_report_plugin.py` | 见下方说明 |

---

## 9. 验证

```powershell
cd D:\Deepseek_Harness\agent-harness\backend

# 单跑 P12（正常看结果）
C:\Users\Administrator\anaconda3\envs\agent_env\python.exe -m pytest tests/test_p12_machine_mounts.py -q

# ⚠️ 看失败详情要加这个插件：backend/conftest.py 的 pytest_sessionfinish 直接 os._exit()，
#    pytest 的失败汇总根本来不及打印（表现为"只有一串点，没有报错详情"）。
$env:PYTHONPATH="."
C:\Users\Administrator\anaconda3\envs\agent_env\python.exe -m pytest tests -o addopts="" -p tests._pytest_report_plugin
Get-Content tests\_pytest_failures.txt

# 端到端（走生产代码路径：真实 .env → 挂载点 → 建 Agent）
python -u ../_probe/p12_machine_probe.py

# 实机对话验证（真调模型 + 真进沙箱；需要 DEEPSEEK_API_KEY + MySQL + 沙箱隧道）
python -u ../_probe/p12_live_chat_probe.py
```

实测结果（本机）：

- **接线**：挂载 `c=C:/, d=D:/, e=E:/`；`write("/mnt/d/.../x.txt")` 落到 `D:\...\x.txt`；
  `ls /mnt` → `/mnt/c`、`/mnt/d`、`/mnt/e`；注册工具 = `delete, edit_file, execute, glob, grep, ls,
  read_file, reset_tool_breakers, task, tavily_search, write_file, write_todos`
  —— **`execute` 与文件工具同时在场**。
- **实机对话**（一轮任务：读 README 首行 → 写 `/workspace/_p12_live.txt` → `execute` 打印）：

  ```
  [tool_call]   read_file  {'file_path': '/mnt/d/Deepseek_Harness/agent-harness/README.md', 'limit': 1}
  [tool_call]   write_file {'file_path': '/workspace/_p12_live.txt', 'content': 'p12-live'}
  [approval]    待审批: ['write_file']        ← HITL 生效（写操作先审批）
  [tool_result] read_file: 1  # Agent Harness …（读的是本机真实文件）
  [tool_result] write_file: Updated file /workspace/_p12_live.txt
  [tool_call]   execute   {'command': 'cat /workspace/_p12_live.txt'}
  [tool_result] execute: p12-live ⏎ [Command succeeded with exit code 0]
  ```

  → **本机文件直读 ✓、写操作要审批 ✓、代码在沙箱容器里执行 ✓、`/workspace` 落盘 ✓**。

---

## 10. 已知边界

- **`/workspace` 别名 route 的重复项（P11 遗留）**：`path=None` 的 `glob`/`grep` 会"搜所有后端"，
  同一个工作区文件既以不带前缀（default 的原始结果）又以带前缀（route remap）出现。
  这是 P11 那条占位 route 的副作用，P12 未改动；挂载点上不会发生（default 不会匹配 `/mnt/**`）。
- **挂载点不参与同步**：本机文件不会被推进容器，容器产出也不会写回 `/mnt/...`（只同步 `/workspace`）。
- **自动挂盘符 ≠ 推荐生产配置**：默认会把 `C:\`（含 `C:\Windows`）也挂进来。给模型用建议改成
  只挂需要的目录（`HARNESS_MACHINE_MOUNTS=proj=D:/work`），并保留 HITL 审批。
- **盘符探测**：自动模式会依次 `is_dir()` 探测 `C:`–`Z:`；断开的网络盘在某些环境可能慢，
  显式写 `HARNESS_MACHINE_MOUNTS` 可完全避免探测。
- **大目录**：`ls`/`glob` 有 5s deadline、`grep` 15s，超时返回截断结果；但 `ls C:\Windows\System32`
  这类调用本身就很慢，模型应尽量下钻到具体目录。
