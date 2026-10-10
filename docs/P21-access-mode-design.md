# P21：权限模式（默认权限（沙箱内） / 完全访问）

> 目标：把"这个智能体到底在你的机器上有多大权限"变成一个**用户可见、可切换**的开关，
> 并且默认落在安全档。前端顶栏一个按钮，后端一套可插拔的执行层。
>
> 代码：`backend/app/agent/access.py`（开关）、`backend/app/agent/sandbox.py::build_main_backend`
> （两种构建路径）、`backend/app/agent/opensandbox_backend.py`（`WorkspacePrefixMixin` /
> `HostShellBackend`）、`backend/app/api/routes/access.py`（接口）、
> `frontend/src/App.vue`（顶栏按钮）。

## 1. 两种模式到底是什么

| | **默认权限（沙箱内）** `sandbox` | **完全访问** `full` |
|---|---|---|
| 文件读写 | 本地磁盘：工作区 `/workspace/**` + 本机 `/mnt/<盘>/**` | 完全相同（共用同一层路径语义） |
| `execute` | **OpenSandbox 容器**（隔离；执行前后做工作区同步） | **本机 shell**（`subprocess`，无隔离、无同步） |
| 访问本机 `/mnt/**` | 读和写都**弹人工审批** | 直接执行，不弹审批 |
| 后端 `default` | `HybridDefaultBackend` | `HostShellBackend`（deepagents `LocalShellBackend` 子类） |
| 是否注册 `execute` 工具 | 是 | 是 |

一句话：**默认权限 = 在沙箱里干活，要碰你电脑上的东西先问我；完全访问 = 就在你这台电脑上干活，不问了。**

## 2. 为什么"文件层一样、只有执行层不同"

原来的实现里 `HybridDefaultBackend` 同时承担了两件事：文件路径前缀折叠 + 沙箱执行。
P21 把第一件事抽成 `WorkspacePrefixMixin`，于是两个后端只在"谁来执行命令"上分叉：

```
        CompositeBackend
        ├── routes: /workspace/ → default；/mnt/<盘>/ → MountedDiskBackend；/mnt/ → MachineRootsBackend
        └── default
             ├── sandbox 模式：HybridDefaultBackend  = WorkspacePrefixMixin + FilesystemBackend
             │                                        + OpenSandboxBackend（execute）
             └── full    模式：HostShellBackend      = WorkspacePrefixMixin + LocalShellBackend
```

这样做的三个好处：

1. **路径语义只有一份**。`/workspace/x` → `<工作区>/x` 的折叠逻辑写在 mixin 里，
   两种模式对模型完全一致（`ls / glob` 的返回路径也一致，有测试钉住）。
2. **完全访问真的是"deepagents 自带的那个后端"**，不是自己另写一套 subprocess 逻辑。
3. `CompositeBackend.execute()` 永远委托 `default`，所以 `execute` 工具自动跟着模式走，
   不需要在两处同步开关。

### 为什么完全访问模式下仍要保留 `/mnt/<盘>/` 路由

`virtual_mode=True` 会把**文件工具**关在工作区目录里。要让文件工具能读写本机其他位置，
必须靠 `/mnt/<盘>/` 这条独立路由（`MountedDiskBackend`）。而 `execute` 本来就能到任何地方
（`shell=True`），两者互补：

- 文件工具：`/mnt/d/proj/a.py`（**虚拟路径**）
- `execute`：`D:\proj\a.py`（**Windows 真实路径**）

⚠️ 这两套写法**不能混**，这正是历史上"模型三套路径各试一遍、单次任务空转 51 次"的成因，
所以 `prompt._FS_TOOLS_FULL` 里把这条规则写死了。

## 3. 审批边界：只有"碰本机"才弹窗

```
工作区 /workspace/**     → 不审批（它就是"沙箱内的地盘"）
本机   /mnt/<盘>/**      → read + write 都触发 interrupt
```

- 规则来源：`sandbox.default_permissions(mode)`；`mode="full"` 直接返回 `[]`。
- 生成机制：deepagents 的 `_build_interrupt_on_from_permissions` 把 `FilesystemPermission`
  翻成每个文件工具的 `when` 谓词，`interrupt` 命中就抛 `Interrupt`。
- `operations=["read","write"]` 都包含 —— 需求原文是"如果需要**访问**本地，那就进入 HITL"，
  所以读也要确认。只想管写入的话，从那一行里去掉 `"read"` 即可（改一行）。
- 噪音提醒：`read` 规则对**批量工具**（`ls`/`glob`/`grep`）是按"搜索子树是否可能与规则重叠"
  判定的，因此 `ls /`、不带 `path=` 的 `grep` 也会触发审批。这是框架行为，
  提示词里已要求"搜索必须带 `path=`"。

### 空规则也必须挂 HITL 中间件

`HARNESS_MACHINE_ACCESS=false` 时 `default_permissions()` 返回**空列表**（没有本机路径可审批）。
此时如果 `interrupt_on` 返回 `None`，`_merge_fs_interrupt_on` 会让
`HumanInTheLoopMiddleware` **根本不进图** —— 图少一个节点，已挂起的中断就会
"隐身"（点同意 → 409 无待审批项）→ 再打开时"复活"。

所以 `permissions.hitl_interrupt_on()` 的判据是「**有没有规则**」而不是「**开关开不开**」：
没规则就给占位键 `{"__hitl_disabled__": False}`，保住图结构。

> 以前总有一条 `/workspace/**` 兜着，这条路径走不到；P21 把审批收窄到本机之后它才会被真正走到。

## 4. 开关的作用域与持久性

| 请求 | 作用域 | 谁能改 |
|------|--------|--------|
| 带 `session_id` | 只影响该会话 | 会话归属者 |
| 不带（默认） | **自己的默认值** | 任何登录用户 |
| 不带 + `scope="global"` | 全局默认 | **仅 admin** |

优先级：**会话 → 用户 → 全局默认**（与 HITL 开关同一套语义）。

- 全局默认来自 `.env` 的 `HARNESS_ACCESS_MODE`（默认 `sandbox`）。
- 运行期只改**内存态**：进程重启回到 `.env` 默认值。这是**刻意**的 ——
  完全访问是个危险档位，重启后自动回落，避免"悄无声息地一直开着"。
- 前端顶栏的按钮走"自己的默认值"这一层（`scope=user`）。

## 5. 热插拔为什么必须重建 Agent

`backend` 与 `permissions` 都是 `create_deep_agent(...)` 的**编译期**输入，
缓存里的 graph 绑死了构建时的后端对象与权限规则。所以：

```
PUT /api/v1/access/config
  → access.set_access_mode(...)
  → runtime.invalidate_agents(user_id=...)   # 丢掉缓存
  → 下一轮对话用新模式重建 graph（正在跑的那一轮不受影响）
```

切到 `full` 时会顺带把**挂起的中断**收尾（`clear_pending_interrupt`）：
审批门都被撤掉了，那些中断再"复活"只会让用户困惑。

## 6. 完全访问模式的两个实现细节（都踩过坑）

### 6.1 `inherit_env=True` 不是可选项

`LocalShellBackend` 默认 `inherit_env=False` 且 `env={}` ⇒ 子进程拿到**空环境变量**，
连 `PATH` 都没有，`python` / `node` / `git` 全部"不是内部或外部命令"。
本机开发场景必须继承环境（沙箱里不存在这个问题，容器有完整系统 PATH）。

### 6.2 中文 Windows 上不能用 `text=True` 拿输出

`LocalShellBackend.execute` 用 `subprocess.run(..., text=True)`，解码走 locale/UTF-8。
中文 Windows 上 `dir` / `ipconfig` 的输出是 **GBK**，解码异常**发生在读取线程里**，
`subprocess.run` 照常返回 ⇒ **stdout 变空、退出码 0** —— 工具把"什么都没输出"当成成功，
熔断器也不会计数，模型只会反复换招。

所以 `HostShellBackend.execute` 自己实现：先拿 bytes，再按
「UTF-8 严格 → 失败退到 `locale.getencoding()`（系统 ANSI 代码页，= cp936）→ `errors="replace"`」
解码。其余语义（超时、`[stderr]` 标注、截断、`Exit code: N`）与父类保持一致。

> ⚠️ 兜底必须用 `locale.getencoding()` 而**不是** `getpreferredencoding(False)`：
> 后者在 UTF-8 模式（`PYTHONUTF8=1`，本项目运行环境就是）下返回 `utf-8`，
> GBK 字节会被全部替换成 U+FFFD（实测 `dir` 的中文全变 `?`）。

## 7. 提示词：两套工具说明段互斥

```
_PROMPT_HEAD + (_FS_TOOLS_SANDBOX | _FS_TOOLS_FULL) + _PROMPT_TAIL
             + (machine_mount_prompt_section() | full_access_prompt_section())
```

两段的**核心事实相反**（"execute 在隔离容器里" vs "execute 就在本机"），
所以是替换而不是追加 —— 让模型同时读到两种说法，它只能靠猜。

`full_access_prompt_section()` 必须交代四件事：

1. `execute` 不再隔离，不要再假设"容器里看不到本机"；
2. 文件工具用虚拟路径 `/mnt/...`，`execute` 用 Windows 真实路径 —— **两套别混**；
3. 工作目录 = 工作区目录，相对路径直接可用；
4. 系统是 Windows / cmd，别用 `ls -la` / `cat` / `python3`。

## 8. 已知限制

- **glob / ls 的路径别名**：`glob("/workspace/**/*.txt", path=None)` 会同时返回
  `/x.txt` 与 `/workspace/x.txt` —— 它们指向同一个物理文件（`/workspace` 是 default 的
  route 别名，见 `sandbox.build_main_backend` 的说明）。语义等价、看起来像重复。
  前端文件面板因此默认从 `/workspace` 开始浏览，不要用 `/`。
- **完全访问模式不持久化**：重启回落 `sandbox`（刻意，见 §4）。
- **远端 MCP / 远端沙箱的限制不变**：完全访问只影响本进程的执行路径，
  不会让"跑在别人机器上的远端工具"突然能读到你本机的文件。

## 9. 验证

```bash
# 后端（基准 366 passed）
env -u PYTHONPATH -u CODEBUDDY_SAFE_DELETE_ENABLED -u CODEBUDDY_SAFE_DELETE_BULK_GUARD \
    -u CODEBUDDY_SAFE_DELETE_SANDBOX -u CODEBUDDY_SAFE_DELETE_BROKER_DELETE \
    python -m pytest -q
# 前端（只做编译检查）
cd frontend && node node_modules/vite/bin/vite.js build --config _verify/vite.build.check.mjs
```

`tests/test_p21_access_mode.py` 覆盖：模式注册表与作用域、两种 backend 的形状与
文件层一致性、本机执行（同时证明没碰沙箱）、审批矩阵、提示词互斥、
`create_deep_agent` + 子智能体的图装配（含 HITL 节点必须存在的结构断言）、
以及 `/api/v1/access/config` 的 401 / 往返 / 400 / 403。
