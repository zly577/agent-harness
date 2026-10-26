"""Shared file operations for workspace-prefixed execution backends."""
from __future__ import annotations

import asyncio
from pathlib import Path

from deepagents.backends.protocol import DeleteResult, EditResult, GlobResult, GrepResult, LsResult, ReadResult, WriteResult
from app.agent.path_mapping import PathMapper, anchored_glob_args


class WorkspacePrefixMixin:
    r"""把「带工作区前缀的工具路径」折进本地根目录的公共路径层。

    ## 为什么需要这一层

    `FilesystemBackend(root_dir=<root>, virtual_mode=True)` 的标准语义是
    `/a.txt` → `<root>/a.txt`（模型看到 `/` 就是 root）。但本项目模型侧的路径统一带
    工作区前缀（`/workspace/a.txt`），而且**这个前缀必须与容器内的工作目录同名** ——
    否则"文件工具看到的路径"和"`execute` 在容器里看到的路径"就不是同一个字符串，
    模型得在两套路径体系之间手工翻译（这正是的老毛病）。

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
