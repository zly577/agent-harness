"""Shared atomic, revision-checked memory writes for API and agent tools."""
import hashlib
import os
import tempfile
from pathlib import Path
from threading import RLock

MEMORY_WRITE_LOCK = RLock()
DETAIL_DIRECTORY = ".agent-memory"


class MemoryConflict(ValueError):
    pass


def memory_path(root: Path, relative: str):
    root = root.resolve()
    path = root / relative
    for candidate in [*path.relative_to(root).parents, path.relative_to(root)]:
        target = root / candidate
        if target.is_symlink() or not target.resolve().is_relative_to(root):
            raise ValueError("长期记忆不能指向工作区外部或符号链接")
    return path


def read_snapshot(path, max_chars=100000):
    if path.is_symlink():
        raise ValueError("长期记忆不能使用符号链接")
    if not path.exists():
        return {"content": "", "revision": "missing", "exists": False}
    if not path.is_file() or path.stat().st_size > max_chars * 4:
        raise ValueError("长期记忆文件过大或不是文本文件")
    try:
        data = path.read_bytes()
        content = data.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise ValueError("长期记忆必须为 UTF-8 文本") from exc
    if len(content) > max_chars:
        raise ValueError(f"长期记忆不能超过 {max_chars} 字")
    return {"content": content, "revision": hashlib.sha256(data).hexdigest(), "exists": True}


def write_snapshot(path, content, revision, max_chars=100000):
    if len(content) > max_chars:
        raise ValueError(f"长期记忆不能超过 {max_chars} 字")
    with MEMORY_WRITE_LOCK:
        if read_snapshot(path, max_chars)["revision"] != revision:
            raise MemoryConflict("长期记忆已被修改，请重新加载后再编辑")
        temporary = None
        try:
            with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", newline="", dir=path.parent,
                                             prefix=".agent-memory-", suffix=".tmp", delete=False) as stream:
                temporary = stream.name
                stream.write(content)
            if read_snapshot(path, max_chars)["revision"] != revision:
                raise MemoryConflict("长期记忆已被修改，请重新加载后再编辑")
            os.replace(temporary, path)
        finally:
            if temporary and os.path.exists(temporary):
                os.unlink(temporary)
        return read_snapshot(path, max_chars)
