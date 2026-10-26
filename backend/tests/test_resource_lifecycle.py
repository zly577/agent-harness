"""Regression checks for storage initialization, cancellation and SSE ownership."""
import asyncio
import io
import threading
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest
from fastapi import HTTPException

from app.agent import context
from app.agent.host_shell import _BoundedCapture
from app.agent.runner import RunManager
from app.api.routes import chat_stream, sessions, files


@pytest.fixture
def isolated_storage(tmp_path, monkeypatch):
    asyncio.run(context.aclose())
    monkeypatch.setattr(context.settings, "sqlite_path", str(tmp_path / "isolated.sqlite"))
    yield
    asyncio.run(context.aclose())


def test_storage_initialization_is_singleton_and_ready(isolated_storage):
    async def scenario():
        connections = await asyncio.gather(*[context._ensure_conn() for _ in range(8)])
        assert len({id(connection) for connection in connections}) == 1
        savers = await asyncio.gather(*[context.get_checkpointer() for _ in range(8)])
        stores = await asyncio.gather(*[context.get_store() for _ in range(8)])
        assert len({id(saver) for saver in savers}) == len({id(store) for store in stores}) == 1
        async with connections[0].execute("SELECT name FROM sqlite_master WHERE type='table'") as cursor:
            tables = {row[0] for row in await cursor.fetchall()}
        assert {"checkpoints", "store"} <= tables
    asyncio.run(scenario())


def test_failed_saver_setup_can_be_retried(isolated_storage, monkeypatch):
    original = context.AsyncSqliteSaver.setup
    calls = 0
    async def setup(self):
        nonlocal calls
        calls += 1
        if calls == 1:
            raise RuntimeError("setup failed")
        return await original(self)
    monkeypatch.setattr(context.AsyncSqliteSaver, "setup", setup)
    async def scenario():
        with pytest.raises(RuntimeError):
            await context.get_checkpointer()
        assert context._saver is None
        assert await context.get_checkpointer() is context._saver
    asyncio.run(scenario())


def test_cancel_during_connect_closes_unpublished_connection(isolated_storage, monkeypatch):
    connection = SimpleNamespace(close=AsyncMock())
    async def scenario():
        entered, release = asyncio.Event(), asyncio.Event()
        async def connect(path):
            entered.set()
            await release.wait()
            return connection
        monkeypatch.setattr(context.aiosqlite, "connect", connect)
        task = asyncio.create_task(context._ensure_conn())
        await entered.wait()
        task.cancel()
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert context._conn is None
        connection.close.assert_awaited_once()
    asyncio.run(scenario())


def test_stop_timeout_retains_task_and_does_not_cancel_cleanup_twice():
    async def scenario():
        manager = RunManager()
        entered, cleaning, release = asyncio.Event(), asyncio.Event(), asyncio.Event()
        cancellations = 0
        async def events():
            nonlocal cancellations
            entered.set()
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                cancellations += 1
                cleaning.set()
                await release.wait()
                raise
            yield "result", "unreachable"
        run = manager.start("sid", {}, event_stream=events)
        try:
            await entered.wait()
            assert not await manager.stop_and_wait("sid", timeout=.01)
            await cleaning.wait()
            assert run.status == "stopping" and manager.is_running("sid")
            assert manager.stop("sid")
            assert not await manager.aclose(timeout=.01)
            assert cancellations == 1 and manager.current("sid") is run
            release.set()
            assert await manager.stop_and_wait("sid")
            assert run.status == "stopped"
        finally:
            release.set()
            await manager.aclose()
    asyncio.run(scenario())


def test_shutdown_keeps_storage_open_when_tasks_remain(monkeypatch):
    from app import main
    from app.agent import runner
    from app.db import bootstrap
    monkeypatch.setattr(main, "_run_migrations", lambda: None)
    monkeypatch.setattr(bootstrap, "run_bootstrap", lambda: None)
    monkeypatch.setattr(runner.run_manager, "aclose", AsyncMock(return_value=False))
    close = AsyncMock()
    monkeypatch.setattr(main, "_close_agent_storage", close)
    async def scenario():
        async with main.lifespan(main.app):
            pass
    with pytest.raises(RuntimeError, match="仍在停止"):
        asyncio.run(scenario())
    close.assert_not_awaited()


def test_stop_endpoint_reports_pending_and_delete_refuses(monkeypatch):
    db = SimpleNamespace(delete=Mock(), commit=Mock())
    user = SimpleNamespace(id="uid")
    monkeypatch.setattr(sessions, "owned_session_or_404", lambda *args: SimpleNamespace(id="sid"))
    monkeypatch.setattr(sessions.run_manager, "stop_and_wait", AsyncMock(return_value=False))
    monkeypatch.setattr(sessions.run_manager, "current", lambda sid: SimpleNamespace(running=True))
    monkeypatch.setattr(sessions.run_manager, "is_running", lambda sid: True)
    response = asyncio.run(sessions.stop_session_run("sid", user, db))
    assert response.status_code == 202 and b'"stopped":false' in response.body
    with pytest.raises(HTTPException) as error:
        asyncio.run(sessions.delete_session("sid", user, db))
    assert error.value.status_code == 409
    db.delete.assert_not_called()


def test_sse_heartbeats_do_not_cancel_the_subscription(monkeypatch):
    monkeypatch.setattr(chat_stream, "HEARTBEAT_SECONDS", .005)
    async def scenario():
        release = asyncio.Event()
        cancelled = False
        async def source():
            nonlocal cancelled
            try:
                await release.wait()
                yield {"type": "result", "content": "done"}
            except asyncio.CancelledError:
                cancelled = True
                raise
        stream = chat_stream.stream_events(source())
        assert (await anext(stream)).startswith(": keep-alive")
        assert (await anext(stream)).startswith(": keep-alive")
        assert not cancelled
        release.set()
        remainder = [frame async for frame in stream]
        assert "result" in remainder[0] and "event: done" in remainder[-1]
    asyncio.run(scenario())


def test_disconnect_releases_only_the_subscription(monkeypatch):
    from app.agent.runner import SessionRun
    monkeypatch.setattr(chat_stream, "HEARTBEAT_SECONDS", .005)
    async def scenario():
        manager = RunManager()
        run = SessionRun(session_id="sid")
        stream = chat_stream.stream_events(manager.subscribe("sid", run=run))
        await anext(stream)
        assert run.subscribers
        await stream.aclose()
        assert not run.subscribers and run.running
    asyncio.run(scenario())


def test_pipe_capture_drains_every_byte_but_retains_only_the_limit():
    class CountingInput(io.BytesIO):
        closed_after_read = False
        def close(self):
            self.closed_after_read = self.tell() == 2_000_000
            super().close()
    stream = CountingInput(b"x" * 2_000_000)
    capture = _BoundedCapture(stream, 1000)
    capture.thread.join(timeout=2)
    content, truncated = capture.snapshot()
    assert capture.done.is_set() and stream.closed_after_read
    assert capture.total == 2_000_000 and len(content) == 1000 and truncated


def test_slow_file_listing_runs_off_the_event_loop(monkeypatch):
    entered, release = threading.Event(), threading.Event()
    def ls(path):
        entered.set()
        assert release.wait(timeout=2)
        return SimpleNamespace(entries=[])
    monkeypatch.setattr(files, "_user_backend", lambda *args: SimpleNamespace(ls=ls))
    async def scenario():
        task = asyncio.create_task(files.list_files("/", SimpleNamespace(id="uid"), None, None, None))
        try:
            for _ in range(100):
                if entered.is_set(): break
                await asyncio.sleep(.001)
            assert entered.is_set() and not task.done()
        finally:
            release.set()
        assert (await task)["entries"] == []
    asyncio.run(scenario())


def test_local_download_streams_and_closes_after_disconnect(tmp_path, monkeypatch):
    from deepagents.backends import CompositeBackend, FilesystemBackend
    from app.services.files import prepare_download
    file = tmp_path / "大文件.txt"
    file.write_bytes(b"a" * 200_000)
    fs = FilesystemBackend(root_dir=tmp_path, virtual_mode=True)
    backend = CompositeBackend(default=fs, routes={"/workspace/": fs})
    monkeypatch.setattr(fs, "download_files", lambda *args: pytest.fail("must not buffer the complete local file"))
    source = prepare_download(backend, "/workspace/大文件.txt")
    assert source.size == 200_000 and source.file is not None
    monkeypatch.setattr(files, "_user_backend", lambda *args: backend)
    monkeypatch.setattr(files, "prepare_download", lambda *args: source)
    async def scenario():
        response = await files.download_file("/workspace/大文件.txt", SimpleNamespace(id="uid"), None, None, None)
        first = await anext(response.body_iterator)
        assert len(first) == 64 * 1024
        await response.body_iterator.aclose()
        assert source.file.closed
    asyncio.run(scenario())


def test_streaming_download_retains_path_traversal_checks(tmp_path):
    from deepagents.backends import FilesystemBackend
    from app.services.files import prepare_download
    fs = FilesystemBackend(root_dir=tmp_path, virtual_mode=True)
    with pytest.raises(ValueError):
        prepare_download(fs, "/../private.txt")
