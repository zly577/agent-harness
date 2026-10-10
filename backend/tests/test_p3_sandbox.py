"""P3 测试：执行路由总开关 + 备用内存虚拟文件系统（不触发真实模型调用）。

说明：P11 起主 backend 变成「本地磁盘读写 + OpenSandbox 执行」的混合后端
（见 test_p11_hybrid_backend.py）。本文件保留 P3 引入的两个契约：
- `execute_allowed()` 是沙箱门禁总开关；
- `build_store_backend()` 仍提供纯内存虚拟文件系统（备用形态，不碰宿主磁盘）。
"""
import asyncio

from app.agent import sandbox
from app.core.config import settings


def test_execute_allowed_follows_sandbox_enabled(monkeypatch):
    """判断路由：总开关跟随 SANDBOX_ENABLED。"""
    monkeypatch.setattr(settings, "sandbox_enabled", False)
    assert sandbox.execute_allowed() is False
    monkeypatch.setattr(settings, "sandbox_enabled", True)
    assert sandbox.execute_allowed() is True


def test_store_backend_has_no_execute():
    """备用 StoreBackend：可写文件、不可执行（不实现 SandboxBackendProtocol）。"""
    from deepagents.middleware.filesystem import supports_execution

    be = sandbox.build_store_backend()
    assert type(be).__name__ == "StoreBackend"
    assert supports_execution(be) is False


def test_storebackend_virtual_fs_write_read_ls_glob():
    """虚拟文件系统：写/读/列目录/glob 全通（InMemoryStore 兼容 async+sync）。"""
    async def _run():
        be = sandbox.build_store_backend()
        # async 写读
        await be.awrite("/notes.txt", "P3 sandbox test")
        data = await be.aread("/notes.txt")
        assert data.file_data["content"] == "P3 sandbox test"
        # 同步 ls / glob（StoreBackend 内部走 sync store.search）
        ls = be.ls("/")
        paths = [e["path"] for e in ls.entries]
        assert "/notes.txt" in paths
        glob = be.glob("*.txt", "/")
        assert glob.matches and glob.matches[0]["path"] == "/notes.txt"
    asyncio.run(_run())
