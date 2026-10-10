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
import locale
import logging
import os
import re
import subprocess
import tempfile
import threading
from dataclasses import dataclass, field
from datetime import timedelta
from pathlib import Path
from typing import Final

from deepagents.backends import (
    DEFAULT_EXECUTE_TIMEOUT,
    CompositeBackend,
    FilesystemBackend,
    LocalShellBackend,
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

from app.core.config import settings

logger = logging.getLogger(__name__)

#: 本机磁盘在模型侧的挂载根：`/mnt/<名称>/...`（P12）。
MACHINE_MOUNT_ROOT: Final[str] = "/mnt"


def anchored_glob_args(pattern: str, path: str | None) -> tuple[str, str | None]:
    """把「绝对模式 + search path」归一成 `FilesystemBackend.glob` 真正认的组合。

    背景（实测，见 `_probe/p12_glob_probe.py`）：`FilesystemBackend.glob` 的匹配是拿
    **相对 search path** 的路径去比对模式的。所以

        glob('/proj/*.py', path='/proj')   →  []            # 绝对模式 vs 相对比对
        glob('*.py',      path='/proj')   →  ['/proj/existing.py']

    而模型给的是绝对路径。这里在调用前把绝对模式按 search path 折成相对模式
    （模式在 search path 之外时，退化成"以根为锚"解释，等价于 path=None 的行为）。

    Args:
        pattern: 已折掉工具前缀的模式（如 `/proj/*.py`；挂载点已折掉 `/mnt/<名>`）。
        path: 已折掉工具前缀的搜索目录（如 `/proj`），或 None。

    Returns:
        `(pattern, path)`：可直接传给 `FilesystemBackend.glob`。
    """
    if not pattern or not pattern.startswith("/"):
        return pattern, path  # 相对模式：本来就是相对 search path 的

    search = (path or "/").rstrip("/") or "/"
    if search == "/":
        # 以根为锚：去掉开头的 `/` 就是"相对根目录"的模式
        return (pattern.lstrip("/") or "**"), "/"
    if pattern == search or pattern.startswith(search + "/"):
        rel = pattern[len(search):].lstrip("/")
        return (rel or "**"), search
    # 模式指向 search path 之外：按根锚解释（忽略 path，比返回空更符合直觉）
    return (pattern.lstrip("/") or "**"), "/"

# ============================================================================
# 路径映射：虚拟绝对路径  <->  沙箱真实路径
# ============================================================================

# 工具/系统目录不参与映射（否则 `ls /bin` 会被改写成 `/workspace/bin`）
_SYSTEM_ROOTS: Final[frozenset[str]] = frozenset(
    {"/bin", "/sbin", "/usr", "/lib", "/lib64", "/etc", "/proc", "/sys", "/dev", "/var", "/opt"}
)
_PATH_TOKEN: Final = re.compile(r"(?<![\w./-])/[A-Za-z0-9_./\-]*")


def translate_command(command: str, root: str) -> str:
    """把命令里的虚拟绝对路径改写成沙箱真实路径。

    例（root=/workspace）：`python /scripts/a.py` → `python /workspace/scripts/a.py`

    跳过：系统目录（`_SYSTEM_ROOTS`）、`/`、`//`、以及已带 root 前缀的路径（避免二次改写）。
    注：装了 `prefix=/workspace` 之后，模型的路径本来就是容器路径，这个函数通常什么都不改；
    它保留是为了兼容 `prefix=None` 的部署形态。
    """
    root = root.rstrip("/")

    def _sub(m: re.Match[str]) -> str:
        tok = m.group(0)
        if tok in ("/", "//") or tok.startswith(root + "/"):
            return tok
        head = "/" + tok.lstrip("/").split("/", 1)[0]
        if head in _SYSTEM_ROOTS:
            return tok
        return root + tok

    return _PATH_TOKEN.sub(_sub, command)


class PathMapper:
    """虚拟路径（模型看到的 `/workspace/a/b.txt`）与沙箱路径（`/workspace/a/b.txt`）互转。

    两种情况：
    - **无工具前缀**（`prefix=None`）：沙箱根 = `settings.sandbox_root`（如 `/workspace`），
      模型的 `/a.txt` 在容器里是 `/workspace/a.txt`。
    - **有工具前缀**（`prefix="/workspace"`）：模型路径本身就带 `/workspace`，容器根仍是
      `/workspace`，所以前缀外的部分是**同一个字符串** —— 模型不需要做路径翻译。
    """

    def __init__(self, root: str, prefix: str | None = None) -> None:
        self.root = "/" + root.strip("/")
        self.prefix = PathMapper._norm(prefix)

    @staticmethod
    def _norm(prefix: str | None) -> str | None:
        if not prefix:
            return None
        norm = "/" + prefix.strip("/")
        return None if norm == "/" else norm

    def to_sandbox(self, virtual_path: str) -> str:
        """虚拟路径 → 沙箱内真实路径（幂等）。"""
        if virtual_path in ("", "/"):
            return self.root
        if virtual_path == self.root or virtual_path.startswith(self.root + "/"):
            return virtual_path  # 已经是容器路径（含带 prefix 的情形）
        if self.prefix and (virtual_path == self.prefix or virtual_path.startswith(self.prefix + "/")):
            # `/workspace/a.txt` → 去掉 prefix 后落到容器根：`/workspace/a.txt`
            return self.root + virtual_path[len(self.prefix):]
        return self.root + "/" + virtual_path.lstrip("/")

    def to_virtual(self, sandbox_path: str) -> str:
        """沙箱内真实路径 → 虚拟路径（带 prefix 的模型视角）。"""
        inner = sandbox_path
        if sandbox_path == self.root:
            inner = "/"
        elif sandbox_path.startswith(self.root + "/"):
            inner = sandbox_path[len(self.root):]
        if self.prefix and inner != "/":
            return self.prefix + inner
        return inner or "/"


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


class WorkspacePrefixMixin:
    r"""把「带工作区前缀的工具路径」折进本地根目录的公共路径层。

    ## 为什么需要这一层

    `FilesystemBackend(root_dir=<root>, virtual_mode=True)` 的标准语义是
    `/a.txt` → `<root>/a.txt`（模型看到 `/` 就是 root）。但本项目模型侧的路径统一带
    工作区前缀（`/workspace/a.txt`），而且**这个前缀必须与容器内的工作目录同名** ——
    否则"文件工具看到的路径"和"`execute` 在容器里看到的路径"就不是同一个字符串，
    模型得在两套路径体系之间手工翻译（这正是 P11 之前的老毛病）。

    于是需要一层很薄的翻译：

    - 进入 backend **之前**把 `/workspace` 折掉（`_strip_prefix`）；
    - 返回给模型 **之前**再补回来（`_display_path`，否则会把宿主绝对路径漏给模型）。

    ## 为什么抽成 mixin

    这层逻辑被两个后端共用，它们的区别**只在"谁来执行命令"**：

    | backend | 文件操作 | execute |
    |---------|---------|---------|
    | `HybridDefaultBackend` | 本地磁盘 | OpenSandbox 容器（默认权限） |
    | `HostShellBackend` | 本地磁盘 | 本机 shell（完全访问） |

    路径语义必须完全一致，所以放在这里；谁执行命令那部分各自实现。
    """

    #: 工具路径前缀（`PathMapper`）；None 表示模型直接看到 `/a.txt`。
    _prefix: "PathMapper | None"

    # ---- 工具路径 → FilesystemBackend 内部的虚拟路径（去掉 prefix） ----
    def _strip_prefix(self, key: str) -> str:
        """把 `/workspace/a.txt` 折成 `/a.txt`；prefix 之外的路径原样返回。

        纯字符串操作（不做文件系统检查）：会被 `_resolve_path`/`ls`/`glob` 高频调用。
        """
        if self._prefix is None or self._prefix.root == "/":
            return key
        root = self._prefix.root
        if key == root:
            return "/"
        if key.startswith(root + "/"):
            return key[len(root):]
        return key

    # ---- 覆写 FilesystemBackend 的全部路径入口，统一先折 prefix ----
    def _resolve_path(self, key: str) -> Path:
        return super()._resolve_path(self._strip_prefix(key))

    def _display_path(self, path: Path) -> str:
        """把本地真实路径还原成带 prefix 的工具路径（否则会把宿主绝对路径泄露给模型）。"""
        if self._prefix is None or self._prefix.root == "/":
            return super()._display_path(path)
        try:
            return self._prefix.root + "/" + path.resolve().relative_to(self.cwd).as_posix()
        except (ValueError, OSError, RuntimeError):
            return path.name or "/"

    def ls(self, path: str) -> LsResult:
        return super().ls(self._strip_prefix(path))

    async def als(self, path: str) -> LsResult:
        return await asyncio.to_thread(self.ls, path)

    def read(self, file_path: str, offset: int = 0, limit: int = 2000) -> ReadResult:
        return super().read(self._strip_prefix(file_path), offset=offset, limit=limit)

    async def aread(self, file_path: str, offset: int = 0, limit: int = 2000) -> ReadResult:
        return await asyncio.to_thread(self.read, file_path, offset, limit)

    def write(self, file_path: str, content: str) -> WriteResult:
        return super().write(self._strip_prefix(file_path), content)

    async def awrite(self, file_path: str, content: str) -> WriteResult:
        return await asyncio.to_thread(self.write, file_path, content)

    def edit(self, file_path: str, old_string: str, new_string: str, replace_all: bool = False) -> EditResult:
        return super().edit(self._strip_prefix(file_path), old_string, new_string, replace_all=replace_all)

    async def aedit(self, file_path: str, old_string: str, new_string: str, replace_all: bool = False) -> EditResult:
        return await asyncio.to_thread(self.edit, file_path, old_string, new_string, replace_all)

    def delete(self, file_path: str) -> DeleteResult:
        return super().delete(self._strip_prefix(file_path))

    async def adelete(self, file_path: str) -> DeleteResult:
        return await asyncio.to_thread(self.delete, file_path)

    def grep(
        self,
        pattern: str,
        path: str | None = None,
        glob: str | None = None,
        *,
        max_count: int | None = None,
        **kwargs,
    ) -> GrepResult:
        return super().grep(
            pattern,
            self._strip_prefix(path) if path else path,
            glob,
            max_count=max_count,
            **kwargs,
        )

    async def agrep(self, pattern: str, path: str | None = None, glob: str | None = None, **kwargs) -> GrepResult:
        return await asyncio.to_thread(self.grep, pattern, path, glob, **kwargs)

    def glob(self, pattern: str, path: str | None = None) -> GlobResult:
        """模式里的 prefix 也要剥掉：`/workspace/**/*.py` → `**/*.py`。

        `FilesystemBackend.glob` 会把首尾的 `/` 去掉再按"相对根目录"匹配，所以带 prefix
        的锚定模式必须先去前缀，否则匹配不到任何文件。

        另外：`path` 给定时，匹配是**相对 search path** 的（实测见 `anchored_glob_args`），
        所以 `glob('/workspace/notes/*.py', '/workspace/notes')` 这种"双绝对"写法也要
        归一化，不然同样是空结果。
        """
        raw_pattern = self._strip_prefix(pattern)
        search = self._strip_prefix(path) if path else None
        sub_pattern, sub_path = anchored_glob_args(raw_pattern, search)
        return super().glob(sub_pattern, sub_path)

    async def aglob(self, pattern: str, path: str | None = None) -> GlobResult:
        return await asyncio.to_thread(self.glob, pattern, path)


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
    """**工作区的父目录**（P18 起语义变化，看下面的说明）。

    P11~P17：这是唯一的读写根目录，默认 `<repo>/workspace`，可用 `HARNESS_WORKSPACE` 覆盖。
    P18：多租户改造后每个用户的工作区是 `<这个目录>/<user_id>/`，所以本函数返回的是
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
# P12：本机磁盘挂载（默认环境 = 本机，只有 execute 进沙箱）
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
    - `HARNESS_MACHINE_ACCESS=false`（默认）→ 空列表，行为与 P11 完全一致（只有工作区）；
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

    def __init__(self, *, root_dir: str | Path, mount_prefix: str) -> None:
        super().__init__(root_dir=root_dir, virtual_mode=True)
        self._mount_prefix = "/" + mount_prefix.strip("/")

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


def build_machine_routes() -> dict[str, BackendProtocol]:
    """构造本机磁盘的 route 表：`/mnt/<名>/` → 该目录的本地磁盘后端。

    `CompositeBackend` 命中 route 后会**剥掉前缀**，所以 `/mnt/d/proj/a.py` 交给
    `MountedDiskBackend(root_dir="D:/")` 时变成 `/proj/a.py` → 真实路径 `D:/proj/a.py`。
    每个挂载点仍用 `virtual_mode=True`，即被关在该盘/该目录内（`..` 与越界绝对路径被拒）。
    """
    mounts = machine_mounts()
    if not mounts:
        return {}
    routes: dict[str, BackendProtocol] = {
        machine_mount_prefix(name): MountedDiskBackend(
            root_dir=path, mount_prefix=machine_mount_prefix(name)
        )
        for name, path in mounts
    }
    # `/mnt` 这一层本身（否则 ls /mnt 会落到工作区，见 MachineRootsBackend 的说明）
    routes[f"{MACHINE_MOUNT_ROOT}/"] = MachineRootsBackend([name for name, _ in mounts])
    return routes


def decode_shell_output(raw: bytes | None) -> str:
    r"""把本机 shell 的原始输出字节解码成文本（**只用于完全访问模式**）。

    ## 为什么不能交给 `subprocess(text=True)`

    `text=True` 走 `locale`/UTF-8 解码，遇到**混编码输出**会直接在读取线程里抛
    `UnicodeDecodeError`。实测（本机 Windows 中文系统）：`dir` 的输出是 GBK，
    而进程默认解码器是 UTF-8 ⇒ 异常发生在 reader 线程里、`subprocess.run` 照常返回，
    结果是 **stdout 变空、退出码 0** —— 工具把"什么都不输出"当成成功回给模型。
    这正是最坏的一类故障（熔断器也不会计数，模型只会反复换招）。

    ## 策略

    1. 先按 UTF-8 严格解码（`python` 子进程、多数 CLI 工具、`PYTHONUTF8=1` 的场景）；
    2. 失败则按**操作系统 ANSI 代码页**（中文 Windows = cp936/GBK）解码，坏字节降级成 `?`。

    ⚠️ 第 2 步必须用 `locale.getencoding()` 而**不是** `getpreferredencoding(False)`：
    后者在 UTF-8 模式（`PYTHONUTF8=1`，本项目的运行环境就是）下会返回 `utf-8`，
    于是 GBK 字节被当成坏字节全部替换成 U+FFFD（实测 `dir` 的中文全变成 `?`）。
    `getencoding()` 拿的是真正的系统代码页，不受 UTF-8 模式影响。

    两种主流来源都能正常显示，且**永不抛异常**。
    """
    if not raw:
        return ""
    try:
        return raw.decode("utf-8")
    except UnicodeDecodeError:
        pass
    try:
        getter = getattr(locale, "getencoding", None)  # Python 3.11+
        fallback = (getter() if getter else locale.getpreferredencoding(False)) or "utf-8"
    except Exception:  # noqa: BLE001 极端环境下拿不到 locale 也不该让命令"失败"
        fallback = "utf-8"
    return raw.decode(fallback, errors="replace")


class HostShellBackend(WorkspacePrefixMixin, LocalShellBackend):
    r"""**完全访问权限**后端：文件走本地磁盘，`execute` 直接在本机 shell 里跑。

    这是「默认权限（沙箱内）」的对照物 —— 两者的**文件层完全相同**（都落本地磁盘、
    都用 `WorkspacePrefixMixin` 折叠 `/workspace` 前缀），区别只在"谁来执行命令"：

    | | 文件操作 | execute | 隔离 |
    |---|---|---|---|
    | `HybridDefaultBackend` | 本地磁盘 | OpenSandbox 容器（+ 前后同步） | 有（容器） |
    | `HostShellBackend`（本类） | 本地磁盘 | **本机 `subprocess`** | **无** |

    ## 为什么继承 `LocalShellBackend` 而不是自己写 subprocess

    1. 它就是 deepagents 自带的"本机 shell"后端，用户要的就是它；
    2. 超时、输出截断（100KB）、stderr 逐行标 `[stderr]`、
       `stdin=DEVNULL`（防 `cat`/`python` 这类命令读 stdin 挂死）这些细节它都处理好了，
       自己写一遍只会漏掉其中几条。

    ## 为什么还需要 `CompositeBackend` 包一层

    模型侧的路径约定带 `/workspace` 前缀（与容器内工作目录同名），这需要
    `WorkspacePrefixMixin` 折叠；另外 `/mnt/<盘>/` 挂载路由要继续可用
    （见 `build_full_access_backend`）。所以本类作为 `CompositeBackend` 的 `default` 使用。

    ## ⚠️ `virtual_mode=True` 只管文件工具，**管不住 shell**

    `virtual_mode` 把 `read_file`/`write_file` 等限制在 `root_dir` 与已挂载路由内；
    但 `execute("cat D:/a.txt")` 照样能读全盘 —— 命令是 `shell=True` 直接交给系统 shell 的。
    这正是"完全访问"的本意，也是它必须**由用户显式选择、且默认关闭**的原因。

    ## ⚠️ `inherit_env` 必须为 True

    `LocalShellBackend` 默认 `inherit_env=False` 且 `env={}` ⇒ 子进程拿到的是**空环境变量**，
    连 `PATH` 都没有，`python`/`node`/`git` 全部"不是内部或外部命令"。本机开发场景下这是
    纯粹的坑（沙箱里不存在这个问题，因为容器有完整的系统 PATH），所以这里默认继承。
    """

    def __init__(
        self,
        *,
        root_dir: str | Path,
        virtual_mode: bool = True,
        timeout: int | None = None,
        inherit_env: bool = True,
        env: dict[str, str] | None = None,
        prefix: str | None = None,
    ) -> None:
        super().__init__(
            root_dir=root_dir,
            virtual_mode=virtual_mode,
            timeout=int(timeout or settings.sandbox_default_timeout_seconds or DEFAULT_EXECUTE_TIMEOUT),
            inherit_env=inherit_env,
            env=env,
        )
        self._prefix = PathMapper(prefix) if prefix else None
        # 让子进程（尤其是 python）统一吐 UTF-8：本机 Windows 默认是 GBK，命令输出经常
        # 混着两种编码。把 python 侧钉死成 UTF-8，未知来源再走 decode_shell_output 的兜底。
        # 用 setdefault —— 用户显式配置的编码优先。
        self._env.setdefault("PYTHONIOENCODING", "utf-8")
        self._env.setdefault("PYTHONUTF8", "1")

    def execute(self, command: str, *, timeout: int | None = None) -> ExecuteResponse:
        r"""在**本机**执行 shell 命令（`shell=True`，工作目录 = 工作区根）。

        与父类 `LocalShellBackend.execute` 的唯一区别是**输出解码**：父类用
        `text=True`，在中文 Windows 上会让 `dir` 这类命令的输出解码异常发生在读取线程里，
        最终表现为"stdout 为空 + 退出码 0"（静默失败）。这里改成先拿 bytes 再
        `decode_shell_output()` 解码，其余语义（超时、`[stderr]` 标注、截断、
        追加 `Exit code: N`）与父类保持一致。

        ⚠️ 命令不做任何路径翻译：这是**本机** shell，模型应当用本机真实路径
        （`D:\proj\a.py` 或 `D:/proj/a.py`），而不是沙箱里的 `/workspace/...`。
        """
        if not command or not isinstance(command, str):
            return ExecuteResponse(
                output="Error: Command must be a non-empty string.",
                exit_code=1,
                truncated=False,
            )

        effective_timeout = timeout if timeout is not None else self._default_timeout
        if effective_timeout <= 0:
            msg = f"timeout must be positive, got {effective_timeout}"
            raise ValueError(msg)

        try:
            result = subprocess.run(  # noqa: S602 本机 shell 是本后端的定义（完全访问）
                command,
                check=False,
                shell=True,
                capture_output=True,
                stdin=subprocess.DEVNULL,  # 防 `cat`/`python` 这类读 stdin 的命令挂死
                timeout=effective_timeout,
                env=self._env,
                cwd=str(self.cwd),
            )
        except subprocess.TimeoutExpired:
            if timeout is not None:
                msg = f"Error: Command timed out after {effective_timeout} seconds (custom timeout). The command may be stuck or require more time."
            else:
                msg = f"Error: Command timed out after {effective_timeout} seconds. For long-running commands, re-run using the timeout parameter."
            return ExecuteResponse(output=msg, exit_code=124, truncated=False)
        except Exception as e:  # noqa: BLE001 执行异常一律摊平成可读结果，不炸整轮对话
            return ExecuteResponse(
                output=f"Error executing command ({type(e).__name__}): {e}",
                exit_code=1,
                truncated=False,
            )

        out_text = decode_shell_output(result.stdout)
        err_text = decode_shell_output(result.stderr)
        parts: list[str] = []
        if out_text:
            parts.append(out_text)
        if err_text.strip():
            parts.extend(f"[stderr] {line}" for line in err_text.strip().split("\n"))
        output = "\n".join(parts) if parts else "<no output>"

        truncated = False
        if len(output) > self._max_output_bytes:
            output = output[: self._max_output_bytes]
            output += f"\n\n... Output truncated at {self._max_output_bytes} bytes."
            truncated = True

        if result.returncode != 0:
            output = f"{output.rstrip()}\n\nExit code: {result.returncode}"

        return ExecuteResponse(output=output, exit_code=result.returncode, truncated=truncated)


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
        # P12：本机磁盘挂载点（模型侧 /mnt/<名>/）。开启 SANDBOX_MOUNT_MACHINE_DIRS 后，
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
