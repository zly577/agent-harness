r"""沙箱与执行路由（本地磁盘读写 + OpenSandbox 执行）。
## 路由总览

模型有 8 个文件系统工具（deepagents 自动注册）：`ls` `read_file` `write_file`
`edit_file` `delete` `glob` `grep` `execute`。本模块决定它们各自落到哪里：

| 场景 | 工具 | 落地位置 | 实现 |
|------|------|----------|------|
| 不需要执行代码 | ls/read_file/write_file/edit_file/delete/glob/grep | **本地磁盘**（工作区 `/workspace` → `HARNESS_WORKSPACE`） | `FilesystemBackend` |
| 读写本机真实文件（可选） | 同上，路径 `/mnt/<盘符>/...` | **本机磁盘**（`C:\`、`D:\` …） | `FilesystemBackend`（独立 route） |
| 需要执行代码 | execute（shell 命令） | **OpenSandbox 容器** | `OpenSandboxBackend` |
| 执行前后 | — | 工作区 ↔ 容器 文件同步 | `HybridDefaultBackend` |

机制：`CompositeBackend.execute()` **不按路径路由，永远委托给 `default`**；
而文件操作按路径前缀路由、未命中走 `default`。于是把 `default` 做成
「本地磁盘为文件层 + 沙箱为执行层」的混合后端，就同时拿到了两种语义。

## 权限模式（本模块的第二个入口）

    `build_main_backend(mode=...)` 有两个分支，工作区路由相同；默认磁盘只读，full 可写：

| mode | default | execute | 谁决定 |
|------|---------|---------|--------|
| `"sandbox"`（默认） | `HybridDefaultBackend` | OpenSandbox 容器 + 双向同步 | `app/agent/access.py` |
| `"full"`（完全访问） | `HostShellBackend` | **本机 shell**（deepagents `LocalShellBackend`） | 同上 |

`full` 分支没有任何隔离，所以它**必须由用户在前端显式选择**（见 `access.py` 的说明）。

## 为什么本地磁盘用 FilesystemBackend 而不是 StoreBackend

- `StoreBackend` 是**内存态**虚拟文件系统（进程重启丢文件），且不可执行；
  它适合那种「只用虚拟 FS 演示」的场景。
- `FilesystemBackend(root_dir=..., virtual_mode=True)` 把模型看到的虚拟绝对路径
  （`/a.txt`）映射到宿主机真实目录（`<root>/a.txt`），**能持久、能下载、能在文件
  管理器里直接看**，还能被 `execute` 通过挂载/同步读到。
- 两者都不可执行，所以 execute 必须由 `SandboxBackendProtocol` 提供 —— 这就是
  OpenSandbox 的位置。

## 沙箱门禁

`execute_allowed()` 是 Harness 侧的总开关（默认跟随 `SANDBOX_ENABLED`）。
即使 `sandbox_enabled=False`，文件读写工具照常可用本地磁盘，只是 execute 返回
一句可读的错误信息（而不是抛异常炸掉整轮对话）。
"""
from app.core.config import settings

# 权限模式（默认权限 / 完全访问）—— 决定 default 是"沙箱执行"还是"本机 shell"。
from app.agent.access import MODE_FULL, MODE_SANDBOX, normalize_mode

# 混合后端（本地磁盘 + OpenSandbox）实现在 opensandbox_backend.py
# 本机磁盘挂载（/mnt/<名>/... 直读直写本机，execute 仍只进沙箱）
# HostShellBackend（完全访问：execute 直接在本机跑）同样实现在那里
from app.agent.opensandbox_backend import (
    DEFAULT_PREFIX,  # noqa: F401  (re-export 供 API/测试使用)
    MACHINE_MOUNT_ROOT,  # noqa: F401
    HostShellBackend,  # noqa: F401  (re-export，便于测试/诊断直接引用)
    HybridDefaultBackend,  # noqa: F401
    build_full_access_backend,
    build_hybrid_backend,
    build_machine_routes,
    local_workspace_root,
    machine_mount_prefix,
    machine_mounts,
    registry,
    sandbox_status,
)

#: 工作区路径前缀（= 容器内的工作目录，默认 `/workspace`）。
WORKSPACE_PREFIX: str = settings.sandbox_root.rstrip("/") or "/workspace"

#: 为满足 deepagents 的「权限路径必须落在某个 route 内」校验而声明的占位路由。
#:
#: 背景：一旦 backend 具备执行能力（我们的 CompositeBackend default 实现了
#: SandboxBackendProtocol），FilesystemMiddleware 会要求 `permissions` 里**每条规则的
#: 每个路径**都以某个 route 前缀开头（`_all_paths_scoped_to_routes`），否则直接
#: NotImplementedError —— 因为 execute 工具本身不吃路径级权限。
#:
#: 这里把 `/workspace/` 声明成一条指向同一后端自身的 route：不影响任何读写位置
#: （文件仍然落在本地磁盘 `<repo>/workspace/...`），只是让 HITL 审批规则
#: （`/workspace/**` 写操作需人工审批）能通过校验。
PERMISSION_ROUTE_PREFIX: str = WORKSPACE_PREFIX + "/"


def _permission_routes():
    """占位路由：`/workspace/` → 与 default 同一个实例（不改变读写位置）。"""
    return {PERMISSION_ROUTE_PREFIX: None}  # 值在 build_main_backend 里填成 default


def _namespace_factory():
    """StoreBackend 命名空间（备用虚拟文件系统用）。"""

    def ns(runtime):
        return ("assistant", "filesystem")

    return ns


def _finish_backend(backend):
    """给 `CompositeBackend` 补上「工作区占位路由」并排好序。

    两件事都必须做，且顺序固定：
    1. `routes["/workspace/"] = default` —— 满足 deepagents「权限路径必须落在某个 route
       前缀内」的校验（见 `PERMISSION_ROUTE_PREFIX` 的说明）；
    2. `sorted_routes` 按前缀长度倒序 —— `CompositeBackend` 用它做前缀匹配，
       不重排的话 `/mnt/` 会先于 `/mnt/d/` 命中。
    """
    backend.routes[PERMISSION_ROUTE_PREFIX] = backend.default
    backend.sorted_routes = sorted(
        backend.routes.items(), key=lambda x: len(x[0]), reverse=True
    )
    return backend


def build_main_backend(
    session_key: str = "default",
    workspace: str | None = None,
    mode: str = MODE_SANDBOX,
):
    r"""主智能体的 backend。**同一套文件层 + 两种执行层**。

    Args:
        session_key: 会话标识；每个会话一个沙箱容器（`mode="full"` 时用不到，但保持签名一致）。
        workspace: 本地磁盘根目录；默认 `local_workspace_root()`（可用
            `HARNESS_WORKSPACE` 覆盖）。
        mode: 权限模式。`"sandbox"`（默认）= 文件落本地 + execute 进容器；
            `"full"` = 文件落本地 + execute 跑本机（deepagents `LocalShellBackend`）。
            由 `app/agent/access.py` 决定，调用方（`runtime.get_agent`）负责传进来。

    Returns:
        `CompositeBackend`：
        - `default` = `HybridDefaultBackend`（sandbox）或 `HostShellBackend`（full），
          **带 `prefix="/workspace"`** → 虚拟 `/workspace/x` → `<root>/x`
          （模型看到的路径 = 容器/本机工作目录里的路径）
        - `routes`  = 本机磁盘挂载点 + `{"/workspace/": 同一个 default}`

    ## 为什么必须这样（否则一定出现"同一文件两份"）

    deepagents 的两条硬约束叠在一起，导致这里没有更"干净"的写法：

    1. **route 必须存在**：一旦 backend 具备执行能力（两个 default 都实现了
       `SandboxBackendProtocol`），`FilesystemMiddleware` 要求 `permissions` 里每条
       路径都落在某个 route 前缀内，否则抛 `NotImplementedError`。实测：删掉
       `/workspace/` 这条 route，`get_agent()` 直接失败（主 Agent 与子智能体的
       `/workspace/**`、`/workspace/notes/**` 都报"不在 route 前缀内"）。
    2. **route 命中后无条件剥前缀**（deepagents `backends/composite.py`
       `_route_for_path`：`suffix = path[len(prefix):]` → `backend_path = "/" + suffix`），
       然后把结果交给 route 指向的那个 backend。

    推论：只要 route 指向**根目录等于工作区目录**的 backend，
    `/workspace/x` 被剥成 `/x` 后又落回**同一个物理文件** —— 与 `/x` 互为别名。
    所以本地磁盘上**不存在**一个与 `/workspace` 对应的目录：
    磁盘的 flat 结构就是这个虚拟挂载的"根"，`prefix` 负责把两者的路径对齐。

    ⚠️ 别名的副作用：`glob`/`ls` 的**结果**里两种写法可能同时出现
    （`/x.txt` 与 `/workspace/x.txt`），它们指向同一个文件、语义等价，但看起来像重复。
    因此前端文件面板的默认路径是 `/workspace`（见 frontend/src/App.vue），
    不要用 `/` 当用户工作区展示。

    ## 本机磁盘挂载（`/mnt/<盘符>/...`）

    `HARNESS_MACHINE_ACCESS=true` 时额外挂上本机磁盘（见 `build_machine_routes()`），
    于是模型可以**直接读写本机真实文件**：

    | 模式 | `/mnt/d/proj/a.py`（文件工具） | `execute` |
    |------|------------------------------|-----------|
    | sandbox | ✅ 读写本机真实文件（**写/读都需人工审批**） | ❌ 容器里看不到 `/mnt/*` |
    | full | ✅ 同上（不审批） | ✅ **本机** shell，直接能读写，路径用 `D:\...` |

    这些 route 指向独立的 `FilesystemBackend`，**与 default 无关**，所以不影响
    `supports_execution()`（仍然是"default 可执行"）。

    ### `/mnt` 在容器里可见吗？—— 由 `SANDBOX_MOUNT_MACHINE_DIRS` 决定

    默认（false）容器里**没有** `/mnt`：`read_file /mnt/d/a.xlsx` 能成功，但
    `execute` 去读同一个路径必然失败。而这个失败**不带 `Error:` 前缀、也没有
    status=error**（deepagents 只把输出拼上 `[Command failed with exit code N]`）⇒
    熔断器判定为"成功"而不计数 ⇒ 模型反复换招空转（实测 51 次）。

    打开该开关（方案 A）后，每个挂载点以**只读**方式挂进容器内的同一路径 `/mnt/<名>`：

        模型看到 `/mnt/d/proj/a.py`  →  容器内也是 `/mnt/d/proj/a.py`
        ⇒ 文件工具与 execute 指向同一个文件、同一个路径字符串

    这从根上消除了「三套路径命名空间不一致」，且大文件不受 32MB 同步上限约束。
    前提：OpenSandbox 服务端与目标文件同机、且运行时支持 host mount。
    """
    resolved = normalize_mode(mode)
    if resolved == MODE_FULL:
        # 完全访问：execute 直接在本机 shell 跑（HostShellBackend）。
        # 不需要同步（文件本来就在本机），也不需要 session_key。
        backend = build_full_access_backend(
            root_dir=workspace,
            routes=build_machine_routes(read_only=False),
            prefix=WORKSPACE_PREFIX,
        )
    else:
        backend = build_hybrid_backend(
            session_key=session_key,
            root_dir=workspace,
            prefix=WORKSPACE_PREFIX,  # 对齐虚拟路径与容器路径，见上面的说明
            routes=build_machine_routes(),
            sync_before_execute=settings.sandbox_sync_enabled,
            sync_after_execute=settings.sandbox_sync_enabled,
        )
    return _finish_backend(backend)


def build_store_backend():
    """备用：纯内存虚拟文件系统（语义，不可执行、不落盘）。

    仅在需要「沙箱被禁用时也不碰宿主磁盘」的部署形态下使用。
    """
    from deepagents.backends import StoreBackend
    from langgraph.store.memory import InMemoryStore

    return StoreBackend(namespace=_namespace_factory(), store=InMemoryStore())


def build_exec_backend(session_key: str = "default", path_prefix: str | None = None):
    """执行后端本体（OpenSandbox 容器包装）。

    现在它被嵌在 `build_main_backend()` 的 default 里，不再单独作为 backend 使用；
    保留此函数供「只想拿沙箱连接跑诊断命令」的场景使用。
    """
    from app.agent.opensandbox_backend import OpenSandboxBackend

    return OpenSandboxBackend(session_key, path_prefix=path_prefix)


def execute_allowed() -> bool:
    """判断路由总开关：是否允许 execute 进沙箱（人工审批后可进一步收紧）。"""
    return bool(settings.sandbox_enabled)


def validate_permissions(backend=None, mode: str = MODE_SANDBOX) -> list[str]:
    """启动期自检：所有权限路径（主 Agent + 每个子智能体）是否都落在 route 前缀内。

    背景：backend 一旦具备执行能力，deepagents 的 `FilesystemMiddleware` 就要求
    `permissions` 里**每条规则的每个路径**都以某个 route 前缀开头，否则抛
    `NotImplementedError`。这个报错在**有子智能体时**才出现（deepagents 会为每个
    subagent 单独建一个 FilesystemMiddleware，用的是父级 backend），排查成本很高。

    所以这里提前把所有权限源都核一遍，返回问题清单（空 = 全部通过）。
    在 `runtime.get_agent()` 里调用，让配置错误在启动/首次对话时就说清楚。

    `mode` 只影响"没传 backend 时按哪种模式现建一个"；权限规则本身来自
    `default_permissions(mode)`。
    """
    from deepagents.middleware.filesystem import _all_paths_scoped_to_routes

    backend = backend if backend is not None else build_main_backend(mode=mode)

    problems: list[str] = []
    sources: list[tuple[str, list]] = [
        ("主 Agent default_permissions()", default_permissions(mode))
    ]

    # 子智能体：deepagents 会继承父级 backend，所以它们的权限同样受这条约束
    from app.agent.subagents import load_all_subagents

    for spec in load_all_subagents():
        perms = spec.get("permissions") or []
        if perms:
            sources.append((f"子智能体 {spec.get('name')!r}", list(perms)))

    for label, perms in sources:
        if perms and not _all_paths_scoped_to_routes(perms, backend):
            bad = [p for rule in perms for p in rule.paths if not any(
                p.startswith(prefix) for prefix in backend.routes
            )]
            problems.append(
                f"{label} 的权限路径不在 route 前缀内: {bad}；"
                f"可用 route 前缀: {list(backend.routes)}"
            )
    return problems


def default_permissions(mode: str = MODE_SANDBOX):
    """默认文件权限（人工审批用）。**只有"碰本机"才需要审批。**

    ## 语义（用户拍板的那条线）

    - **工作区 `/workspace/**`：完全自由** —— 它就是"沙箱内的地盘"（本地磁盘上的
      工作区目录 + 容器里的同名目录），读写删都不弹窗。
    - **本机真实文件 `/mnt/<盘>/**`：读和写都要人工审批** —— 这是"如果需要访问本地，
      那就进入 HITL 让用户确认"这条需求的落点（`operations=["read", "write"]`）。

    `mode="full"`（完全访问）返回**空列表**：用户已经显式选择了"在本机随便干"，
    再弹窗只会自相矛盾。注意空列表并不等于"图里没有 HITL 节点"——
    `permissions.hitl_interrupt_on()` 会补一个永不匹配的占位键来保住图结构
    （见该模块的头注释，那里记着"弹窗复活 / 点同意报 409"的完整成因）。

    ## 约束（实测）

    一旦 backend 具备执行能力（两个 default 都实现了 `SandboxBackendProtocol`，
    `supports_execution` 为真），deepagents 会要求**所有权限路径都落在某个 route 前缀内**
    （见 `PERMISSION_ROUTE_PREFIX`），否则抛 `NotImplementedError`。
    路径须用 **glob 通配模式**（`/mnt/d/**`）才能匹配子路径。

    本机挂载点只有在 `HARNESS_MACHINE_ACCESS=true` 时才是 route —— 所以关掉本机访问时
    这个函数返回空列表（没有本机可访问，也就没有什么需要审批）。这也是
    `validate_permissions()` 至今不会报"权限路径不在 route 内"的原因。

    是否真的启用审批由 `app.agent.permissions` 的运行期开关决定（热插拔）：
    开关关闭时构建 Agent 传的是**空权限列表**。所以构建 Agent 时应调用
    `permissions.agent_permissions()` 而不是直接调本函数。

    ⚠️ 关于读操作的噪音：`read` 规则对**批量工具**（ls/glob/grep）是按"搜索子树是否可能
    与规则重叠"判定的，所以 `ls /`、不带 `path=` 的 `grep` 也会触发审批。这是框架行为，
    提示词里已要求模型"搜索必须带 path="（见 `prompt.machine_mount_prompt_section`）。
    """
    from deepagents.middleware.filesystem import FilesystemPermission

    if normalize_mode(mode) == MODE_FULL:
        return []

    rules = []
    for name, _path in machine_mounts():
        rules.append(
            FilesystemPermission(
                # 读也要审批：需求原文是"如果需要**访问**本地，那就进入 HITL 让用户确认"。
                # 只想管写入的话，把 "read" 从这里删掉即可（一行的事）。
                operations=["read", "write"],
                paths=[machine_mount_prefix(name) + "**"],
                mode="interrupt",
            )
        )
    return rules


__all__ = [
    "DEFAULT_PREFIX",
    "MACHINE_MOUNT_ROOT",
    "MODE_FULL",
    "MODE_SANDBOX",
    "PERMISSION_ROUTE_PREFIX",
    "WORKSPACE_PREFIX",
    "HostShellBackend",
    "HybridDefaultBackend",
    "build_exec_backend",
    "build_full_access_backend",
    "build_hybrid_backend",
    "build_main_backend",
    "build_store_backend",
    "default_permissions",
    "execute_allowed",
    "local_workspace_root",
    "machine_mount_prefix",
    "machine_mounts",
    "normalize_mode",
    "registry",
    "sandbox_status",
    "validate_permissions",
]
