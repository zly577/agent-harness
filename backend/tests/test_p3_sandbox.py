"""P3 测试：虚拟文件系统沙箱与执行路由（不触发真实模型调用）。"""
import asyncio

from app.agent import sandbox


def test_execute_disabled_by_default():
    """判断路由：默认 execute 未授权。"""
    assert sandbox.execute_allowed() is False
    assert sandbox.build_exec_backend() is None


def test_build_main_backend_is_storebackend():
    """默认 backend 应为 StoreBackend（可写文件、不可执行）。"""
    be = sandbox.build_main_backend()
    assert type(be).__name__ == "StoreBackend"
    assert not hasattr(be, "execute"), "StoreBackend 不应暴露 execute"


def test_storebackend_virtual_fs_write_read_ls_glob():
    """虚拟文件系统：写/读/列目录/glob 全通（InMemoryStore 兼容 async+sync）。"""
    async def _run():
        be = sandbox.build_main_backend()
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
