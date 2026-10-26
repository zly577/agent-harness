"""Resolve local downloads using the same backend routing and path checks."""
import os
import stat
from dataclasses import dataclass
from typing import BinaryIO

from deepagents.backends import CompositeBackend, FilesystemBackend


@dataclass
class DownloadSource:
    size: int
    file: BinaryIO | None = None
    content: bytes | None = None


def prepare_download(backend, path: str) -> DownloadSource:
    selected, key = backend, path
    while isinstance(selected, CompositeBackend):
        selected, key = selected._get_backend_and_key(key)
    if isinstance(selected, FilesystemBackend):
        resolved = selected._resolve_path(key)
        if not stat.S_ISREG(resolved.stat().st_mode):
            raise FileNotFoundError("只能下载普通文件")
        fd = os.open(resolved, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0) | getattr(os, "O_BINARY", 0))
        try:
            metadata = os.fstat(fd)
            if not stat.S_ISREG(metadata.st_mode):
                raise FileNotFoundError("只能下载普通文件")
            stream = os.fdopen(fd, "rb")
        except BaseException:
            os.close(fd)
            raise
        return DownloadSource(size=metadata.st_size, file=stream)
    # Non-filesystem backends retain their existing download contract.
    results = backend.download_files([path])
    if not results or results[0].error or results[0].content is None:
        raise FileNotFoundError("文件不存在")
    content = results[0].content
    return DownloadSource(size=len(content), content=content)
