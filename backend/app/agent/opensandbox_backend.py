"""OpenSandbox 执行后端 + 本地磁盘读写 + 两者同步（deepagents 判断路由）。

## 要解决的问题

deepagents 只有**一个** `backend` 参数，而我们要的是两种完全不同的语义：

1. **不需要执行代码时 → 本地磁盘读写**：`write_file` / `read_file` / `edit_file` /
   `glob` / `grep` 直接落在宿主机磁盘上（用户能看见、能持久、能下载），不碰沙箱。
2. **需要执行代码时 → OpenSandbox 沙箱执行**：`execute` 在远程容器里跑，结果
   （stdout/stderr + 退出码）作为 ToolMessage 回给大模型，容器里产生的文件同步回本地。

## 为什么能这么接（deepagents 0.7.1 的三个关键机制）

- `CompositeBackend(default=..., routes={...})`：**文件操作按路径前缀路由**，未命中路由的
  路径走 `default`。把 `default` 设为本地磁盘后端，文件读写就全走本地磁盘。
- `CompositeBackend.execute()` **不按路径路由，永远委托给 `default`**
  （见 deepagents/backends/composite.py:749-785）。
- `supports_execution(backend)`：当 `backend` 是 `CompositeBackend` 时，判断
  `isinstance(backend.default, SandboxBackendProtocol)`；为 True 才**注册 execute 工具**，
  并且 `execute` 工具内部调用的是 `CompositeBackend.execute`（即我们的沙箱）。
  → 所以：`default = 本地磁盘 + 沙箱执行能力的混合后端`，而 `routes` 留给虚拟路径。

因此本模块的组合是：

```
   模型工具调用 ──▶┌── CompositeBackend ───────────────────────────────────┐
                   │ default = HybridDefaultBackend                        │
                   │   · 文件操作：FilesystemBackend(root_dir=<本地目录>)   │──▶ 本地磁盘
                   │   · execute ：① 默认权限 → OpenSandboxBackend（远程容器）│──▶ 沙箱/本机
                   │                ② 完全访问 → LocalShellBackend（本机 shell）│
                   │   · 执行前后自动做 本地 ↔ 沙箱 文件同步                 │
                   │ routes = {"/workspace/": default}（满足权限 route 校验）│
                   └───────────────────────────────────────────────────────┘
```

`HybridDefaultBackend`（默认权限）与 `HostShellBackend`（完全访问）共用同一个路径层
`WorkspacePrefixMixin`（把模型侧 `/workspace/...` 折进本地根目录），只有执行层不同：

| 类 | 执行层 | 用在 |
|----|--------|------|
| `HybridDefaultBackend` | `OpenSandboxBackend`（容器）+ 双向同步 | 默认权限（沙箱内） |
| `HostShellBackend` | deepagents `LocalShellBackend`（本机 `subprocess`） | 完全访问权限 |

两者都实现 `SandboxBackendProtocol` ⇒ `supports_execution()` 为真 ⇒ deepagents 自动注册
`execute` 工具，命令由 `CompositeBackend.execute()` 路由到各自的执行层。
构建入口是 `sandbox.build_main_backend(mode=...)`，模式开关见 `app/agent/access.py`。

## 执行结果的闭环（仅默认权限模式）

`execute` 结束后做一次「拉回」：把沙箱里**新增/被修改**的文件回写到本地磁盘。
这样大模型看到的文件系统始终只有一份真相（本地磁盘），而沙箱只是「一次性算力」。
完全访问模式下命令本来就在本机跑，不存在"同步"这一步。
"""
from __future__ import annotations

import asyncio
import atexit
import logging
import os
import re
import shlex
import tempfile
import threading
from dataclasses import dataclass, field
from datetime import timedelta
from pathlib import Path
from typing import Final

from deepagents.backends import (
    CompositeBackend,
    FilesystemBackend,
)
from deepagents.backends.protocol import (
    BackendProtocol,
    DeleteResult,
    EditResult,
    ExecuteResponse,
    FileDownloadResponse,
    FileInfo,
    FileUploadResponse,
    GlobResult,
    GrepResult,
    LsResult,
    ReadResult,
    SandboxBackendProtocol,
    WriteResult,
)

from app.agent.host_shell import HostShellBackend, decode_shell_output  # compatibility exports
from app.agent.path_mapping import PathMapper, anchored_glob_args, translate_command
from app.agent.workspace_files import WorkspacePrefixMixin
from app.core.config import settings

logger = logging.getLogger(__name__)

#: 本机磁盘在模型侧的挂载根：`/mnt/<名称>/...`。
MACHINE_MOUNT_ROOT: Final[str] = "/mnt"


# ============================================================================
# 沙箱实例池：按 session 复用容器
# ============================================================================


@dataclass
class _SandboxEntry:
    """一个已就绪沙箱的连接信息 + 本地/沙箱两侧的同步基线。"""

    client: object
    connection_key: str
    mapper: PathMapper
    #: 创建该容器时的**挂载配置指纹**（`sandbox_mount_key()`）。volumes 在创建期就固定了，
    #: 所以配置一变只能换新容器 —— `SandboxRegistry.get()` 靠它判断要不要重建。
    #: 空串 = 本次创建没有挂载任何宿主目录（历史行为）。
    volumes_key: str = ""
    #: 上一次推送时的本地文件指纹 {虚拟路径: (mtime_ns, size)}；下次只推送与它不同的文件
    local_state: dict[str, tuple[int, int]] = field(default_factory=dict)
    #: 执行前快照的沙箱清单 {虚拟路径: (mtime_epoch, size)}；执行后与它 diff 出「产出文件」
    remote_state: dict[str, tuple[float, int]] = field(default_factory=dict)


def _ensure_sandbox_root(client, root: str) -> None:
    """确保沙箱内的工作目录存在。

    沙箱镜像（如 `python:3.11`）默认**不含** `/workspace`，而 `execute` 以它为
    工作目录。若不预先创建，新会话里「本地工作区为空 ⇒ 无文件可推送 ⇒ 目录仍不存在」
    时，每条命令都会失败：

        invalid request, validation error working directory does not exist: /workspace

    只有在模型先写过一个文件（触发同步、顺带建出目录）之后才恢复正常，所以这里
    在创建沙箱后立刻补建一次，让 `execute` 从第一条命令起就可用。
    """
    from opensandbox.models.execd import RunCommandOpts

    target = "/" + root.strip("/")
    if target == "/":
        return
    try:
        client.commands.run(  # type: ignore[attr-defined]
            f"mkdir -p {shlex.quote(target)}",
            opts=RunCommandOpts(working_directory="/"),
        )
    except Exception as e:  # noqa: BLE001
        # 建目录失败不阻断沙箱创建：后续写文件仍可能把目录带出来。
        logger.warning("[sandbox] 预创建工作目录 %s 失败: %s", target, e)


class SandboxRegistry:
    """进程内沙箱连接池（每 session 一个沙箱，懒创建、atexit 统一销毁）。

    不用全局单例沙箱：不同会话/不同用户之间必须隔离。
    """

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._entries: dict[str, _SandboxEntry] = {}
        atexit.register(self.shutdown)

    # ---- 配置 ----
    @staticmethod
    def connection_key() -> str:
        return f"{settings.sandbox_protocol}://{settings.sandbox_domain}#{settings.sandbox_image}"

    def is_enabled(self) -> bool:
        return bool(getattr(settings, "sandbox_enabled", False))

    # ---- 获取连接 ----
    def get(self, session_key: str, path_prefix: str | None = None) -> _SandboxEntry:
        """取（或建）该会话的沙箱连接；失败直接抛异常，由上层转成工具错误信息。"""
        key = session_key or "default"
        wanted = sandbox_mount_key()
        with self._lock:
            entry = self._entries.get(key)
            if entry is not None and entry.volumes_key == wanted and self._alive(entry):
                return entry
            if entry is not None:
                # 两种都必须重建：① 沙箱已死（TTL 到期/被回收）；② 挂载配置变了 ——
                # volumes 只在创建期生效，改配置只能换新容器（否则是"配置改了没反应"）。
                if entry.volumes_key != wanted:
                    logger.info(
                        "[sandbox] 挂载配置变更，重建会话 %s 的容器：%s → %s",
                        key,
                        entry.volumes_key or "(无挂载)",
                        wanted or "(无挂载)",
                    )
                self._entries.pop(key, None)
            entry = self._create(key, path_prefix)
            self._entries[key] = entry
            return entry

    @staticmethod
    def _alive(entry: _SandboxEntry) -> bool:
        try:
            return bool(entry.client.is_healthy())  # type: ignore[attr-defined]
        except Exception:
            return False

    @staticmethod
    def _create(key: str, path_prefix: str | None = None) -> _SandboxEntry:
        """调用 opensandbox SDK 创建容器。

        延迟 import：沙箱未启用时不需要装 opensandbox 包，也不拖慢启动。

        网络形态决定两个参数：
        - `domain`：客户端**能访问到**的 OpenSandbox 服务地址。远端部署时填服务器
          IP / SSH 隧道端口，不能填 127.0.0.1（那是客户端自己的回环）。
        - `use_server_proxy`：容器 IP 客户端不可达时（docker 私有网段、跨机）必须为
          True —— 执行命令与传文件都经服务端代理，客户端只需能访问 domain 一个地址。
        """
        import json

        from opensandbox.config import ConnectionConfigSync
        from opensandbox.sync import SandboxSync

        extra_headers: dict[str, str] = {}
        if settings.sandbox_headers:
            try:
                extra_headers = {str(k): str(v) for k, v in json.loads(settings.sandbox_headers).items()}
            except (ValueError, TypeError, AttributeError) as e:
                logger.warning("[sandbox] SANDBOX_HEADERS 解析失败（应为 JSON 对象）: %s", e)

        conn = ConnectionConfigSync(
            api_key=settings.sandbox_api_key or None,
            domain=settings.sandbox_domain,
            protocol=settings.sandbox_protocol,
            # 创建沙箱是"拉镜像 + 起容器"的长操作；这里必须给足，否则首次创建必然超时
            request_timeout=timedelta(seconds=settings.sandbox_create_timeout_seconds),
            use_server_proxy=bool(settings.sandbox_use_server_proxy),
            headers=extra_headers,
        )
        create_kwargs: dict = {
            "timeout": timedelta(minutes=settings.sandbox_ttl_minutes),
            "ready_timeout": timedelta(seconds=max(settings.sandbox_ready_timeout_seconds, 60)),
            "resource": {"cpu": settings.sandbox_cpu, "memory": settings.sandbox_memory},
            "metadata": {"harness.session": key},
            "connection_config": conn,
        }

        # 方案 A：把本机挂载点只读挂进容器，容器内路径与模型侧 /mnt/<名> 一致。
        volumes = _build_sandbox_volumes()
        try:
            client = SandboxSync.create(
                settings.sandbox_image, volumes=volumes or None, **create_kwargs
            )
        except Exception as e:  # noqa: BLE001
            if not volumes or not _mount_fallback_worthwhile(e):
                raise
            # SDK 注明 host mount「仅当运行时支持时可用」。挂载失败**不能**静默降级成
            # 一切照常 —— 否则用户以为挂载生效了，而模型依旧在容器里找不到 /mnt，
            # 又回到「execute 反复失败但不熔断」的老故障。所以这里大声记一笔。
            logger.exception(
                "[sandbox] 带 %d 个挂载点创建沙箱失败，已退回**无挂载**容器："
                "容器内看不到 /mnt，execute 访问 /mnt 仍会失败。"
                "请确认 ① OpenSandbox 运行时支持 host mount；② SANDBOX_DOMAIN 与目标文件同机。",
                len(volumes),
            )
            client = SandboxSync.create(settings.sandbox_image, **create_kwargs)
            volumes = []

        # 沙箱镜像默认没有工作目录；必须先建出来，否则空工作区下 execute 全部失败。
        _ensure_sandbox_root(client, settings.sandbox_root)

        logger.info(
            "[sandbox] created sandbox for session=%s id=%s domain=%s proxy=%s volumes=%d",
            key,
            getattr(client, "id", "?"),
            settings.sandbox_domain,
            settings.sandbox_use_server_proxy,
            len(volumes),
        )
        if volumes:
            logger.info(
                "[sandbox] 已挂载宿主目录（只读=%s）: %s",
                bool(getattr(settings, "sandbox_mount_read_only", True)),
                ", ".join(f"{host} → {cont}" for host, cont in sandbox_mount_specs()),
            )
        return _SandboxEntry(
            client=client,
            connection_key=key,
            mapper=PathMapper(settings.sandbox_root, prefix=path_prefix),
            # 记录**实际生效**的挂载：若上面降级过，volumes 已清空 ⇒ 指纹为空 ⇒
            # 后续每次 get() 都会重试带挂载创建（配置没改，但环境可能已修好）。
            volumes_key=sandbox_mount_key() if volumes else "",
        )

    # ---- 回收 ----
    def destroy(self, session_key: str) -> None:
        with self._lock:
            entry = self._entries.pop(session_key or "default", None)
        if entry is not None:
            self._safe_destroy(entry)

    def shutdown(self) -> None:
        with self._lock:
            entries = list(self._entries.values())
            self._entries.clear()
        for e in entries:
            self._safe_destroy(e)

    @staticmethod
    def _safe_destroy(entry: _SandboxEntry) -> None:
        try:
            entry.client.destroy()  # type: ignore[attr-defined]
        except Exception as e:  # 回收失败不影响主流程
            logger.warning("[sandbox] destroy failed: %s", e)


registry = SandboxRegistry()


# ============================================================================
# OpenSandbox 执行后端（SandboxBackendProtocol）
# ============================================================================


class OpenSandboxBackend(SandboxBackendProtocol):
    """把 OpenSandbox 容器包装成 deepagents 可识别的执行后端。

    设计上**只实现 execute / upload_files / download_files / id** 这四个必需方法；
    其余文件操作（ls/read/write/edit/glob/grep）交给 `HybridDefaultBackend` 走本地磁盘，
    不浪费远程往返。
    """

    def __init__(self, session_key: str, path_prefix: str | None = None) -> None:
        self._session_key = session_key or "default"
        self._path_prefix = path_prefix

    # ---- 必需：唯一标识 ----
    @property
    def id(self) -> str:
        entry = self._entry
        return f"opensandbox:{self._session_key}:{getattr(entry.client, 'id', 'unknown')}"

    @property
    def _entry(self) -> _SandboxEntry:
        return registry.get(self._session_key, self._path_prefix)

    # ---- 必需：执行 ----
    def execute(self, command: str, *, timeout: int | None = None) -> ExecuteResponse:
        """在沙箱里执行 shell 命令，返回合并的 stdout/stderr + 退出码。

        注意：BaseSandbox 的 glob/grep/read 等辅助实现也走 execute（以 shell 命令形式），
        但本后端的文件操作已被上层改道到本地磁盘，所以这里只承载「模型真的写了命令」的场景。
        """
        from opensandbox.models.execd import RunCommandOpts

        entry = self._entry
        mapper = entry.mapper
        translated = translate_command(command, mapper.root)
        opts = RunCommandOpts(
            working_directory=mapper.root,
            timeout=timedelta(seconds=timeout or settings.sandbox_default_timeout_seconds),
        )
        try:
            execution = entry.client.commands.run(translated, opts=opts)  # type: ignore[attr-defined]
        except Exception as e:
            logger.warning("[sandbox] execute failed: %s", e)
            return ExecuteResponse(output=f"Error: sandbox execution failed: {e}", exit_code=1)

        return ExecuteResponse(
            output=_render_output(execution),
            exit_code=_exit_code(execution),
            truncated=bool(getattr(execution, "truncated", False)),
        )

    async def aexecute(self, command: str, *, timeout: int | None = None) -> ExecuteResponse:
        """异步版本：SDK 是同步阻塞 HTTP，用 to_thread 避免卡住事件循环。

        LangChain 会优先调用 async 实现，所以这里必须显式提供，否则默认实现虽然
        也会走 to_thread，但会丢掉 timeout 语义。
        """
        return await asyncio.to_thread(self.execute, command, timeout=timeout)

    # ---- 必需：文件传输（供本地↔沙箱同步使用） ----
    def upload_files(self, files: list[tuple[str, bytes]]) -> list[FileUploadResponse]:
        entry = self._entry
        out: list[FileUploadResponse] = []
        for path, content in files:
            sandbox_path = entry.mapper.to_sandbox(path)
            try:
                entry.client.files.write_file(sandbox_path, content)  # type: ignore[attr-defined]
                out.append(FileUploadResponse(path=path))
            except Exception as e:  # 单个文件失败不影响其它文件（协议要求部分成功）
                logger.warning("[sandbox] upload %s failed: %s", sandbox_path, e)
                out.append(FileUploadResponse(path=path, error=str(e)))
        return out

    async def aupload_files(self, files: list[tuple[str, bytes]]) -> list[FileUploadResponse]:
        return await asyncio.to_thread(self.upload_files, files)

    def download_files(self, paths: list[str]) -> list[FileDownloadResponse]:
        entry = self._entry
        out: list[FileDownloadResponse] = []
        for path in paths:
            sandbox_path = entry.mapper.to_sandbox(path)
            try:
                content = entry.client.files.read_bytes(sandbox_path)  # type: ignore[attr-defined]
                out.append(FileDownloadResponse(path=path, content=content))
            except Exception as e:
                logger.warning("[sandbox] download %s failed: %s", sandbox_path, e)
                out.append(FileDownloadResponse(path=path, error=str(e)))
        return out

    async def adownload_files(self, paths: list[str]) -> list[FileDownloadResponse]:
        return await asyncio.to_thread(self.download_files, paths)

    # ---- 供同步层使用：列沙箱内文件清单 ----
    def list_files(self) -> dict[str, tuple[float, int]]:
        """返回沙箱根目录下所有文件的 {虚拟路径: (mtime_epoch, size)}。

        实现选型：不用 `files.search`（其 pattern 语义由服务端实现决定，容易漏掉嵌套文件），
        直接用 `find`（容器内自带 GNU find）—— `%T@` 给 epoch 秒、`%s` 给字节数，
        输出稳定且一次遍历拿到整棵树，供拉回时做 diff。
        """
        entry = self._entry
        root = entry.mapper.root
        cmd = f"find {root} -type f -printf '%T@\\t%s\\t%p\\n' 2>/dev/null"
        try:
            execution = entry.client.commands.run(cmd)  # type: ignore[attr-defined]
        except Exception as e:
            logger.warning("[sandbox] list_files failed: %s", e)
            return {}

        out: dict[str, tuple[float, int]] = {}
        for line in (_render_output(execution) or "").splitlines():
            parts = line.split("\t", 2)
            if len(parts) != 3:
                continue
            raw_mtime, raw_size, raw_path = parts
            try:
                out[entry.mapper.to_virtual(raw_path)] = (float(raw_mtime), int(raw_size))
            except ValueError:
                continue
        return out

    def list_dir(self, virtual_path: str = "/") -> list[dict]:
        """列出容器内目录（诊断用）：[{path, is_dir, size, modified_at}]。"""
        from opensandbox.models.filesystem import DirectoryListEntry

        entry = self._entry
        target = entry.mapper.to_sandbox(virtual_path)
        try:
            infos = entry.client.files.list_directory(  # type: ignore[attr-defined]
                DirectoryListEntry(path=target)
            )
        except Exception as e:
            logger.warning("[sandbox] list_dir %s failed: %s", target, e)
            return []
        return [
            {
                "path": entry.mapper.to_virtual(str(getattr(i, "path", ""))),
                "is_dir": str(getattr(i, "entry_type", "")).lower() in ("dir", "directory"),
                "size": int(getattr(i, "size", 0) or 0),
                "modified_at": getattr(i, "modified_at", None).isoformat()
                if getattr(i, "modified_at", None)
                else "",
            }
            for i in infos or []
        ]


def _render_output(execution) -> str:
    """把 opensandbox Execution 归一为一段给大模型看的文本。"""
    parts: list[str] = []
    try:
        text = getattr(execution, "text", None)
        if isinstance(text, str) and text:
            parts.append(text)
        else:
            logs = getattr(execution, "logs", None)
            if logs is not None:
                for msg in getattr(logs, "stdout", None) or []:
                    parts.append(str(getattr(msg, "text", "")))
                stderr = [str(getattr(m, "text", "")) for m in (getattr(logs, "stderr", None) or [])]
                if stderr:
                    parts.append("[stderr]\n" + "".join(stderr))
            for res in getattr(execution, "result", None) or []:
                t = getattr(res, "text", None)
                if t:
                    parts.append(str(t))
    except Exception as e:  # 解析失败也不能把主流程炸掉
        return f"Error: failed to render sandbox output: {e}"
    output = "".join(parts)
    return output or "(no output)"


def _exit_code(execution) -> int | None:
    code = getattr(execution, "exit_code", None)
    if code is None:
        complete = getattr(execution, "complete", None)
        code = getattr(complete, "exit_code", None) if complete is not None else None
    try:
        return int(code) if code is not None else None
    except (TypeError, ValueError):
        return None


# ============================================================================
# 混合 default 后端：文件→本地磁盘，execute→OpenSandbox，并做同步
# ============================================================================


class HybridDefaultBackend(WorkspacePrefixMixin, FilesystemBackend, SandboxBackendProtocol):
    """`CompositeBackend` 的 default：文件操作落本地磁盘，execute 进沙箱。

    继承顺序很重要：
    - `FilesystemBackend` 在前 → `ls/read/write/edit/delete/glob/grep` 全部是本地磁盘实现；
    - 同时实现 `SandboxBackendProtocol` → `supports_execution()` 为真，deepagents 才会
      注册 execute 工具，并把 `CompositeBackend.execute()` 路由到这里的 `execute`。

    同步策略：
    - `execute` **之前**：把本地「自上次同步后新增/修改」的文件推送到沙箱；
    - `execute` **之后**：把沙箱里新增/修改的文件拉回本地磁盘。
    两侧清单都存在 `_SandboxEntry` 里，所以只在真正有变化时才传输。

    `prefix` 的作用（可选，默认 None）：把工具路径前缀折进本地目录。
    例如 `prefix="/workspace"`、`root_dir=<repo>/workspace` 时：

        工具路径 `/workspace/a.txt`  →  本地 `<repo>/workspace/a.txt`

    这样"文件工具看到的路径"和"execute 在容器里看到的路径"是**同一个字符串**，
    模型不需要在两种路径体系之间做翻译（`prefix` 也会自动叠到沙箱路径上）。
    """

    #: 单次同步的总体积上限（防止把大目录塞进沙箱）
    SYNC_MAX_BYTES: Final = 32 * 1024 * 1024
    #: 同步时忽略的目录/文件（相对路径，posix 分隔）
    SYNC_IGNORE: Final[frozenset[str]] = frozenset(
        {".git", ".venv", "venv", "__pycache__", "node_modules", ".mypy_cache", ".pytest_cache", ".ruff_cache"}
    )

    def __init__(
        self,
        *,
        session_key: str,
        root_dir: str | Path,
        virtual_mode: bool = True,
        sync_before_execute: bool = True,
        sync_after_execute: bool = True,
        prefix: str | None = None,
    ) -> None:
        super().__init__(root_dir=root_dir, virtual_mode=virtual_mode)
        self._session_key = session_key or "default"
        self._sync_before = sync_before_execute
        self._sync_after = sync_after_execute
        self._path_prefix = prefix
        self._sandbox = OpenSandboxBackend(session_key, path_prefix=prefix)
        self._prefix = PathMapper(prefix) if prefix else None

    # 路径层（_strip_prefix / ls / read / write / edit / delete / grep / glob 及其
    # 异步版本）全部来自 `WorkspacePrefixMixin` —— 与完全访问后端共用同一套路径语义。
    # 本类只负责"怎么执行"。

    # ---- 沙箱可用性 ----
    def is_sandbox_available(self) -> bool:
        """沙箱是否已启用（未启用时 execute 返回可读错误，而不是抛异常）。"""
        return registry.is_enabled()

    # ---- 执行 ----
    def execute(self, command: str, *, timeout: int | None = None) -> ExecuteResponse:
        if not self.is_sandbox_available():
            return ExecuteResponse(
                output=(
                    "Error: 沙箱执行未启用（SANDBOX_ENABLED=0）。"
                    "文件读写工具（read_file/write_file/edit_file/glob/grep）仍可正常使用本地磁盘。"
                ),
                exit_code=1,
            )
        try:
            # 1) 本地 → 沙箱：把模型刚才写在本地磁盘的文件送进容器，让命令能读到
            if self._sync_before:
                self.push_local_changes()

            # 2) 执行前拍一次沙箱快照（remote_before），作为「这次执行产出了什么」的基线
            entry = self._entry()
            remote_before = self._sandbox.list_files() if self._sync_after else {}

            # 3) 真正的沙箱执行（stdout/stderr + 退出码 → 回给大模型）
            result = self._sandbox.execute(command, timeout=timeout)

            # 4) 沙箱 → 本地：把产出/被改动的文件拉回磁盘，保证「文件只有一份真相」
            if self._sync_after:
                pulled = self.pull_sandbox_changes(remote_before)
                if pulled:
                    logger.debug("[sandbox] pulled %d file(s) back to local disk", len(pulled))
            return result
        except Exception as e:
            logger.warning("[sandbox] execute path failed: %s", e)
            return ExecuteResponse(output=f"Error: sandbox unavailable: {e}", exit_code=1)

    async def aexecute(self, command: str, *, timeout: int | None = None) -> ExecuteResponse:
        """异步入口：整个「同步→执行→拉回」都在线程里跑（SDK 是同步 HTTP）。"""
        return await asyncio.to_thread(self.execute, command, timeout=timeout)

    # ---- 同步实现 ----
    def _entry(self) -> _SandboxEntry:
        return registry.get(self._session_key, self._path_prefix)

    def local_manifest(self) -> dict[str, tuple[int, int]]:
        """扫描本地根目录，返回 {虚拟路径: (mtime_ns, size)}。"""
        out: dict[str, tuple[int, int]] = {}
        root = Path(self.cwd)
        if not root.exists():
            return out
        for p in root.rglob("*"):
            try:
                if not p.is_file():
                    continue
                if any(part in self.SYNC_IGNORE for part in p.relative_to(root).parts):
                    continue
                st = p.stat()
                out["/" + p.relative_to(root).as_posix()] = (st.st_mtime_ns, st.st_size)
            except OSError:
                continue
        return out

    def _local_path(self, virtual_path: str) -> Path:
        """虚拟路径 → 本地磁盘真实路径。

        注意：虚拟路径带 `/workspace` 前缀，但本地根目录**不含**这一层
        （前缀在 `_strip_prefix` 里已被折掉），所以这里也要剥掉后缀前缀，
        否则会写成 `<root>/workspace/x`。
        """
        return Path(self.cwd) / self._strip_prefix(virtual_path).lstrip("/")

    def push_local_changes(self) -> list[str]:
        """把本地新增/修改过的文件推到沙箱，返回推送的虚拟路径列表。"""
        entry = self._entry()
        current = self.local_manifest()
        changed = [p for p, sig in current.items() if entry.local_state.get(p) != sig]

        batch: list[tuple[str, bytes]] = []
        total = 0
        for virt in changed:
            try:
                data = self._local_path(virt).read_bytes()
            except OSError:
                continue
            if total + len(data) > self.SYNC_MAX_BYTES:
                logger.warning("[sandbox] push skipped %s (sync size cap reached)", virt)
                break
            total += len(data)
            batch.append((virt, data))

        if batch:
            responses = self._sandbox.upload_files(batch)
            failed = [r.path for r in responses if r.error]
            if failed:
                logger.warning("[sandbox] push failed for %d file(s): %s", len(failed), failed[:5])
            pushed = [r.path for r in responses if not r.error]
        else:
            pushed = []

        # 记录推送基线：下次只推送「之后又被改动」的文件
        entry.local_state = current
        return pushed

    def pull_sandbox_changes(self, remote_before: dict[str, tuple[float, int]] | None = None) -> list[str]:
        """把沙箱里新增/修改（相对执行前快照）的文件拉回本地磁盘。

        只在沙箱侧做 diff（执行前后两次清单比较），不与本地 mtime 混比 —— 容器和宿主机的
        时钟/文件系统语义不同，跨域比较 mtime 会误判（把沙箱产物当成"旧文件"而不拉回）。
        """
        entry = self._entry()
        remote_after = self._sandbox.list_files()
        if not remote_after:
            return []

        baseline = remote_before if remote_before is not None else entry.remote_state
        to_pull = [p for p, sig in remote_after.items() if baseline.get(p) != sig]

        written: list[str] = []
        if to_pull:
            responses = self._sandbox.download_files(to_pull)
            for resp in responses:
                if resp.error or resp.content is None:
                    continue
                target = self._local_path(resp.path)
                try:
                    target.parent.mkdir(parents=True, exist_ok=True)
                    target.write_bytes(resp.content)
                    written.append(resp.path)
                except OSError as e:
                    logger.warning("[sandbox] pull %s failed: %s", resp.path, e)

        # 拉回后本地多出了这些文件 → 刷新两侧基线，避免下一轮把刚拉回的产物又原样推回沙箱
        entry.remote_state = remote_after
        entry.local_state = self.local_manifest()
        return written

    # ---- 沙箱内文件的便捷访问（供 Harness API / 诊断使用） ----
    def sandbox_entries(self, virtual_path: str = "/") -> list[dict]:
        """列出容器内某个目录（绕过 deepagents 工具层，直接问沙箱）。

        用于「本地文件 vs 容器文件」对账：排查同步问题时最有用的一件工具。
        """
        return self._sandbox.list_dir(virtual_path)


# ============================================================================
# 对外构建函数
# ============================================================================


def local_workspace_root() -> Path:
    """**工作区的父目录**（语义变化，看下面的说明）。

    这是唯一的读写根目录，默认 `<repo>/workspace`，可用 `HARNESS_WORKSPACE` 覆盖。
    多租户改造后每个用户的工作区是 `<这个目录>/<user_id>/`，所以本函数返回的是
    **父目录**，具体用户的工作区请用 `app.core.workspace.workspace_for(user_id)`。

    保留这个函数名（而不是直接删掉）有两个原因：
    - `prompt.py` / 测试 / 探针里还有引用，改语义比改名字更不容易漏；
    - 它同时是"搬迁旧数据"判定的基准目录（见 `app/core/workspace.py`）。
    """
    from app.core.workspace import workspace_base

    return workspace_base()


#: 默认工具路径前缀：模型看到 `/workspace/...`，与容器工作目录同名，避免路径翻译。
DEFAULT_PREFIX: Final[str | None] = settings.sandbox_root or None


# ============================================================================
# 本机磁盘挂载（默认环境 = 本机，只有 execute 进沙箱）
# ============================================================================

#: 自动挂载盘符时扫描的字母（跳过 A:/B: 这类保留盘符）
_DRIVE_LETTERS: Final[str] = "CDEFGHIJKLMNOPQRSTUVWXYZ"


def machine_mount_prefix(name: str) -> str:
    """挂载名的 route 前缀：`d` → `/mnt/d/`（末尾带 `/`，供 CompositeBackend 前缀匹配）。"""
    return f"{MACHINE_MOUNT_ROOT}/{name}/"


def _unique_mount_name(raw: str, used: set[str]) -> str:
    """把盘符/目录名归一成一个安全的挂载名（小写字母数字下划线）；重名自动加序号。"""
    name = re.sub(r"[^A-Za-z0-9_]+", "_", (raw or "")).strip("_").lower() or "root"
    candidate, i = name, 2
    while candidate in used:
        candidate = f"{name}{i}"
        i += 1
    return candidate


def machine_mounts() -> list[tuple[str, Path]]:
    """本机磁盘挂载点：`[(挂载名, 本机真实路径)]`，模型侧路径 = `/mnt/<挂载名>/...`。

    三种情况：
    `HARNESS_MACHINE_ACCESS=false`（默认）→ 空列表，行为与完全一致（只有工作区）；
    - `HARNESS_MACHINE_ACCESS=true` 且 `HARNESS_MACHINE_MOUNTS` 为空 → 自动挂载本机存在的
      所有盘符（`c=C:/`、`d=D:/` …），即"整个本机"；
    - 显式配置 → 解析逗号分隔的 `名称=路径` 或裸路径（裸路径用盘符/目录名当挂载名）。

    注意这里**只提供访问入口**：挂载点上的文件不会进沙箱容器，`execute` 仍然只能看到
    `/workspace`（见 HybridDefaultBackend 的同步逻辑）。
    """
    if not getattr(settings, "harness_machine_access", False):
        return []

    raw_cfg = (getattr(settings, "harness_machine_mounts", "") or "").strip()
    mounts: list[tuple[str, Path]] = []
    used: set[str] = set()

    if raw_cfg:
        for item in raw_cfg.split(","):
            item = item.strip()
            if not item:
                continue
            if "=" in item:
                alias, _, path_text = item.partition("=")
                alias = alias.strip()
            else:
                alias, path_text = "", item
            try:
                path = Path(path_text.strip()).expanduser().resolve()
            except (OSError, RuntimeError):  # 无法解析的路径直接跳过（不让配置错误炸掉建 Agent）
                logger.warning("[machine] 挂载点无法解析，已跳过: %s", item)
                continue
            if not path.is_dir():
                logger.warning("[machine] 挂载点不存在或不是目录，已跳过: %s", path)
                continue
            name = _unique_mount_name(alias or (path.drive.rstrip(":") or path.name), used)
            used.add(name)
            mounts.append((name, path))
        return mounts

    # 自动模式：本机所有存在的盘符
    for letter in _DRIVE_LETTERS:
        root = Path(f"{letter}:/")
        try:
            if not root.is_dir():
                continue
        except OSError:  # 空光驱/断开的网络盘可能抛错
            continue
        name = _unique_mount_name(letter, used)
        used.add(name)
        mounts.append((name, root))
    return mounts


# ============================================================================
# 方案 A：把本机挂载点**只读挂载进沙箱容器**
#
# 要解决的问题：`/mnt/<名>/...` 只是宿主文件工具的 route，容器里看不到它。
# 结果是模型 `read_file /mnt/d/a.xlsx` 成功，紧接着用 execute 去处理它却必然失败；
# 而 execute 的失败返回既不带 `Error:` 前缀也没有 status=error（deepagents 只把输出
# 拼上 `[Command failed with exit code N]`），熔断器判定为"成功"而不计数
# ⇒ 模型反复换招空转（实测一次任务里 execute 20 次 + MCP 19 次 + read_file 12 次）。
#
# 做法：把挂载点挂进容器，**容器内路径沿用模型侧的 `/mnt/<名>`**，于是文件工具与
# execute 看到的是同一个文件、同一个路径字符串 —— 路径命名空间不一致从根上消失，
# 大文件也不再受 32MB 同步上限约束。
# ============================================================================


#: 与「挂载被拒」无关的创建失败 —— 这些不值得退回无挂载重试（见 _mount_fallback_worthwhile）。
_MOUNT_UNRELATED_ERRORS: Final[frozenset[str]] = frozenset(
    {
        "SandboxConnectionException",  # 服务端不可达 / 网络错误
        "SandboxTimeoutException",  # 请求超时
        "SandboxReadyTimeoutException",  # 等就绪超时（镜像在拉）
        "SandboxRateLimitException",  # 被限流
    }
)


def sandbox_mount_specs() -> list[tuple[str, str]]:
    """容器挂载规格：`[(宿主绝对路径, 容器内路径)]`；未开启时返回空列表。

    纯函数（不 import SDK、不建容器），供单测与 `/health` 展示复用。
    """
    if not getattr(settings, "sandbox_mount_machine_dirs", False):
        return []
    return [
        (path.as_posix(), f"{MACHINE_MOUNT_ROOT}/{name}") for name, path in machine_mounts()
    ]


def sandbox_mount_key() -> str:
    """挂载配置指纹（进沙箱缓存签名）。

    为什么必须进签名：`volumes` 是**容器创建期**参数，改配置不会影响已存在的容器 ——
    不参与签名就会出现「配置改了、行为没变」的假象（同一个坑：沙箱按会话缓存）。
    """
    specs = sandbox_mount_specs()
    if not specs:
        return ""
    read_only = bool(getattr(settings, "sandbox_mount_read_only", True))
    return f"ro={read_only};" + "|".join(f"{host}=>{cont}" for host, cont in specs)


def _mount_fallback_worthwhile(exc: BaseException) -> bool:
    """带挂载创建失败后，是否值得**退回无挂载**再试一次？

    只有「服务端明确拒绝这次请求」才值得重试。**网络 / 超时 / 限流与挂载无关** ——
    重试只会白等一轮（创建本身已有 4 次内部重试 + 退避，一次连接失败就要好几秒）。
    实测：服务端没起时会抛 `SandboxConnectionException`，此时去做降级重试纯属浪费。

    反过来，宁可多试一次也不要把沙箱彻底搞挂：所以除下面这几类之外的异常
    （`SandboxApiException` / `InvalidArgumentException` / 其它）都按"可能是挂载被拒"处理。
    """
    return type(exc).__name__ not in _MOUNT_UNRELATED_ERRORS


def _build_sandbox_volumes() -> list:
    """规格 → SDK `Volume` 列表（延迟 import，未启用沙箱时不装 opensandbox 也能跑）。"""
    specs = sandbox_mount_specs()
    if not specs:
        return []

    from opensandbox.models.sandboxes import Host, Volume

    read_only = bool(getattr(settings, "sandbox_mount_read_only", True))
    volumes: list = []
    for host_path, container_path in specs:
        name = container_path.rstrip("/").rsplit("/", 1)[-1] or "mount"
        try:
            host = Host(path=host_path)
        except Exception as e:  # noqa: BLE001 单个挂载点非法不该拖垮建容器
            logger.warning("[sandbox] 挂载点非法，已跳过 %s: %s", host_path, e)
            continue
        volumes.append(
            Volume(
                name=f"machine-{name}",
                host=host,
                mount_path=container_path,
                read_only=read_only,
            )
        )
    return volumes


class MountedDiskBackend(FilesystemBackend):
    """一个本机磁盘挂载点的文件后端（`/mnt/<名>/` route 指向它）。

    `CompositeBackend` 命中 route 后会把**路径**剥掉前缀再交给 route 后端，但
    **`glob` 的模式串是原样透传的**（`composite.glob` 只对 `path` 做 route 折算，只有
    `path=None`/`"/"` 那条分支才会用 `_strip_route_from_pattern`）。于是
    `glob(pattern="/mnt/d/proj/*.py", path="/mnt/d/proj")` 会把模式当"相对根目录"去匹配
    proj 下的 `mnt/d/proj/*.py`，**一个文件都匹配不到**。

    这里把挂载前缀从 `pattern`/`path` 上统一折掉，让 glob 与其它工具的路径语义一致
    （与 `HybridDefaultBackend` 对 `/workspace` 前缀做的是同一件事）。
    """

    def __init__(self, *, root_dir: str | Path, mount_prefix: str, read_only: bool = False) -> None:
        super().__init__(root_dir=root_dir, virtual_mode=True)
        self._mount_prefix = "/" + mount_prefix.strip("/")
        self._read_only = read_only

    def _write_error(self) -> str:
        return "默认模式下本机磁盘只读；请切换完全访问模式后修改，或将产出写到 /workspace。"

    def write(self, file_path: str, content: str) -> WriteResult:
        if self._read_only:
            return WriteResult(error=self._write_error())
        return super().write(file_path, content)

    def edit(self, file_path: str, old_string: str, new_string: str, replace_all: bool = False) -> EditResult:
        if self._read_only:
            return EditResult(error=self._write_error())
        return super().edit(file_path, old_string, new_string, replace_all)

    def delete(self, file_path: str) -> DeleteResult:
        if self._read_only:
            return DeleteResult(error=self._write_error())
        return super().delete(file_path)

    def upload_files(self, files: list[tuple[str, bytes]]) -> list[FileUploadResponse]:
        if self._read_only:
            return [FileUploadResponse(path=path, error="permission_denied") for path, _ in files]
        return super().upload_files(files)

    def _strip_mount_prefix(self, value: str | None) -> str | None:
        """`/mnt/tmp/proj/*.py` → `/proj/*.py`；`/mnt/tmp` → `/`；其余原样。"""
        if not value:
            return value
        root = self._mount_prefix
        if value == root:
            return "/"
        if value.startswith(root + "/"):
            return value[len(root):]
        return value

    def glob(self, pattern: str, path: str | None = None) -> GlobResult:
        raw_pattern = self._strip_mount_prefix(pattern) or pattern
        search = self._strip_mount_prefix(path) if path else None
        sub_pattern, sub_path = anchored_glob_args(raw_pattern, search)
        return super().glob(sub_pattern, sub_path)


class MachineRootsBackend(FilesystemBackend):
    """`/mnt` 这一层的占位后端：只回答 `ls`（列出已挂载的盘符/目录）。

    为什么需要它：`CompositeBackend` 只在「路径等于 `/`」或「命中 route 前缀」时聚合
    route，`/mnt` 本身既不是 `/` 也不命中 `/mnt/d/` 这类前缀，会落到 default（本地工作区）
    —— 模型 `ls /mnt` 就会看到一堆与盘符无关的工作区文件。用这个只读占位后端把这一层补上。

    其余操作一律给出可读错误（引导模型用具体的 `/mnt/<名称>/...`），不会静默写到别处。
    """

    def __init__(self, names: list[str], mount_root: str = MACHINE_MOUNT_ROOT) -> None:
        # root_dir 永远不会被用到（ls 覆写、_resolve_path 直接报错），给个存在的空目录即可
        super().__init__(root_dir=Path(tempfile.gettempdir()), virtual_mode=True)
        self._names = list(names)
        self._mount_root = mount_root.rstrip("/") or MACHINE_MOUNT_ROOT

    def _hint(self) -> str:
        listed = ", ".join(f"{self._mount_root}/{n}/" for n in self._names) or "（无）"
        return f"{self._mount_root} 只是本机磁盘的挂载目录，请使用具体挂载点：{listed}"

    def _resolve_path(self, key: str) -> Path:
        raise ValueError(self._hint())

    def ls(self, path: str) -> LsResult:
        return LsResult(
            entries=[
                FileInfo(path=f"/{name}", is_dir=True, size=0, modified_at="")
                for name in self._names
            ]
        )

    # 其余操作一律返回可读错误（而不是抛异常/静默写到别处）：
    # 模型只可能误用 `/mnt` 这一层（正确用法是 `/mnt/<名称>/...`）。
    def read(self, file_path: str, offset: int = 0, limit: int = 2000) -> ReadResult:
        return ReadResult(error=self._hint())

    def write(self, file_path: str, content: str) -> WriteResult:
        return WriteResult(error=self._hint())

    def edit(self, file_path: str, old_string: str, new_string: str, replace_all: bool = False) -> EditResult:
        return EditResult(error=self._hint())

    def delete(self, file_path: str) -> DeleteResult:
        return DeleteResult(error=self._hint())

    def glob(self, pattern: str, path: str | None = None) -> GlobResult:
        return GlobResult(matches=[], error=None)

    def grep(self, pattern: str, path: str | None = None, glob: str | None = None, **kwargs) -> GrepResult:
        return GrepResult(matches=[], error=None)


def build_machine_routes(*, read_only: bool = True) -> dict[str, BackendProtocol]:
    """构造本机磁盘的 route 表：`/mnt/<名>/` → 该目录的本地磁盘后端。

    `CompositeBackend` 命中 route 后会**剥掉前缀**，所以 `/mnt/d/proj/a.py` 交给
    `MountedDiskBackend(root_dir="D:/")` 时变成 `/proj/a.py` → 真实路径 `D:/proj/a.py`。
    每个挂载点用 virtual_mode 限制在该目录内；默认只读，full 模式传 read_only=False。
    """
    mounts = machine_mounts()
    if not mounts:
        return {}
    routes: dict[str, BackendProtocol] = {
        machine_mount_prefix(name): MountedDiskBackend(
            root_dir=path, mount_prefix=machine_mount_prefix(name), read_only=read_only
        )
        for name, path in mounts
    }
    # `/mnt` 这一层本身（否则 ls /mnt 会落到工作区，见 MachineRootsBackend 的说明）
    routes[f"{MACHINE_MOUNT_ROOT}/"] = MachineRootsBackend([name for name, _ in mounts])
    return routes


def build_full_access_backend(
    *,
    root_dir: str | Path | None = None,
    routes: dict[str, BackendProtocol] | None = None,
    prefix: str | None = DEFAULT_PREFIX,
    timeout: int | None = None,
) -> CompositeBackend:
    """构建「完全访问」backend：本机文件 + 本机 shell（`HostShellBackend`）。

    与 `build_hybrid_backend` 的签名刻意保持对称 —— 调用方（`sandbox.build_main_backend`）
    只按权限模式二选一，其余参数含义完全一致。

    Args:
        root_dir: 本地磁盘根目录（= 该用户的工作区），默认 `local_workspace_root()`。
        routes: 额外虚拟路径路由（本机磁盘挂载点 `/mnt/<盘>/`、`/workspace/` 占位路由）。
        prefix: 工具路径前缀，默认 `/workspace`。
        timeout: 单条命令超时（秒），默认取 `SANDBOX_DEFAULT_TIMEOUT_SECONDS`。

    Returns:
        `CompositeBackend`；`default` 具备执行能力（`LocalShellBackend` 实现了
        `SandboxBackendProtocol`），所以 deepagents 会照常注册 `execute` 工具，
        只是命令跑在本机而不是容器里。
    """
    root = Path(root_dir) if root_dir is not None else local_workspace_root()
    root.mkdir(parents=True, exist_ok=True)

    default = HostShellBackend(
        root_dir=root,
        virtual_mode=True,
        timeout=timeout,
        inherit_env=True,
        prefix=prefix,
    )
    return CompositeBackend(default=default, routes=dict(routes or {}))


def build_hybrid_backend(
    *,
    session_key: str = "default",
    root_dir: str | Path | None = None,
    routes: dict[str, BackendProtocol] | None = None,
    prefix: str | None = DEFAULT_PREFIX,
    sync_before_execute: bool = True,
    sync_after_execute: bool = True,
) -> CompositeBackend:
    """构建「本地磁盘读写 + OpenSandbox 执行」的混合 backend。

    Args:
        session_key: 会话标识；每个会话一个沙箱容器。
        root_dir: 本地磁盘根目录，默认 `local_workspace_root()`。
        routes: 额外虚拟路径路由（如 `{"/memories/": StoreBackend(...)}`）。
            注意：这些路由里的文件**不在**沙箱可见范围内（沙箱只镜像 root_dir）。
        prefix: 工具路径前缀，默认 `/workspace`（= `settings.sandbox_root`）。
            设成 `None` 则模型直接看到 `/a.txt`，由同步层翻译成容器内的
            `/workspace/a.txt`。
        sync_before_execute: 执行前是否把本地改动推送进沙箱。
        sync_after_execute: 执行后是否把沙箱产出拉回本地磁盘。

    Returns:
        可直接传给 `create_deep_agent(backend=...)` 的 `CompositeBackend`。
    """
    root = Path(root_dir) if root_dir is not None else local_workspace_root()
    root.mkdir(parents=True, exist_ok=True)

    default = HybridDefaultBackend(
        session_key=session_key,
        root_dir=root,
        virtual_mode=True,
        sync_before_execute=sync_before_execute,
        sync_after_execute=sync_after_execute,
        prefix=prefix,
    )
    return CompositeBackend(default=default, routes=dict(routes or {}))


def sandbox_status() -> dict[str, object]:
    """沙箱配置/可用性摘要（供 /health 或前端展示）。"""
    return {
        "enabled": registry.is_enabled(),
        "domain": settings.sandbox_domain,
        "protocol": settings.sandbox_protocol,
        "use_server_proxy": bool(settings.sandbox_use_server_proxy),
        "image": settings.sandbox_image,
        "root": settings.sandbox_root,
        "ttl_minutes": settings.sandbox_ttl_minutes,
        # 本机磁盘挂载点（模型侧 /mnt/<名>/）。开启 SANDBOX_MOUNT_MACHINE_DIRS 后，
        # 这些目录同时只读挂进容器 ⇒ execute 也能按同一路径读到。
        "machine_access": bool(getattr(settings, "harness_machine_access", False)),
        "machine_mounts": [
            {"name": name, "path": str(path), "virtual": machine_mount_prefix(name)}
            for name, path in machine_mounts()
        ],
        "mount_into_sandbox": bool(getattr(settings, "sandbox_mount_machine_dirs", False)),
        "mount_read_only": bool(getattr(settings, "sandbox_mount_read_only", True)),
        "sandbox_volumes": [
            {"host": host, "container": cont} for host, cont in sandbox_mount_specs()
        ],
    }


__all__ = [
    "MACHINE_MOUNT_ROOT",
    "HostShellBackend",
    "HybridDefaultBackend",
    "MachineRootsBackend",
    "MountedDiskBackend",
    "OpenSandboxBackend",
    "PathMapper",
    "SandboxRegistry",
    "WorkspacePrefixMixin",
    "anchored_glob_args",
    "build_full_access_backend",
    "build_hybrid_backend",
    "build_machine_routes",
    "decode_shell_output",
    "local_workspace_root",
    "machine_mount_prefix",
    "machine_mounts",
    "sandbox_mount_key",
    "sandbox_mount_specs",
    "registry",
    "sandbox_status",
    "translate_command",
]
