"""Chat transports share one run, cancellation and replay lifecycle; no model calls."""
import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from fastapi import HTTPException
from langgraph.types import Command

from app.agent import runtime
from app.agent.runner import RunConflict, RunManager, RunStopped, SessionRun
from app.api.routes import chat, chat_stream
from app.schemas.chat import ChatRequest
from app.services import chat as service


@pytest.fixture
def manager(monkeypatch):
    manager = RunManager()
    for module in (service, chat, chat_stream):
        monkeypatch.setattr(module, "run_manager", manager)
    monkeypatch.setattr(service, "owned_session_or_404", lambda *args: SimpleNamespace(title="existing"))
    monkeypatch.setattr(runtime, "touch_session", lambda *args: None)
    monkeypatch.setattr(runtime, "get_agent", AsyncMock())
    monkeypatch.setattr(service, "read_pending_approval", AsyncMock(return_value={"pending": False}))
    return manager


def test_json_and_sse_exclude_each_other(manager, monkeypatch):
    async def scenario():
        entered, release = asyncio.Event(), asyncio.Event()
        async def events(*args, **kwargs):
            entered.set()
            await release.wait()
            yield "result", "完成"
        monkeypatch.setattr(runtime, "agent_event_stream", events)
        req, user = ChatRequest(message="hello", session_id="sid"), SimpleNamespace(id="uid")
        try:
            first = asyncio.create_task(chat.chat(req, user, None))
            await entered.wait()
            with pytest.raises(HTTPException) as error:
                await chat_stream.chat_stream(req, user, None)
            assert error.value.status_code == 409
            release.set()
            assert (await first).reply == "完成"
            entered.clear()
            release.clear()
            response = await chat_stream.chat_stream(req, user, None)
            await entered.wait()
            # The session is reserved even before the response is consumed.
            with pytest.raises(HTTPException) as error:
                await chat.chat(req, user, None)
            assert error.value.status_code == 409
            release.set()
            output = "".join([chunk async for chunk in response.body_iterator])
            assert '"type": "session"' in output and "完成" in output and "event: done" in output
        finally:
            await manager.aclose()
    asyncio.run(scenario())


def test_disconnected_json_waiter_does_not_cancel_run(manager, monkeypatch):
    async def scenario():
        entered, release = asyncio.Event(), asyncio.Event()
        async def events(*args, **kwargs):
            yield "token", "部分"
            entered.set()
            await release.wait()
            yield "result", "部分和完整回复"
        monkeypatch.setattr(runtime, "agent_event_stream", events)
        run = manager.start("sid", {})
        waiter = asyncio.create_task(manager.wait_result(run))
        try:
            await entered.wait()
            waiter.cancel()
            with pytest.raises(asyncio.CancelledError):
                await waiter
            assert run.running and not run.task.cancelled()
            release.set()
            assert await manager.wait_result(run) == "部分和完整回复"
            replay = [event async for event in manager.subscribe("sid")]
            assert [event["type"] for event in replay] == ["token", "result"]
        finally:
            await manager.aclose()
    asyncio.run(scenario())


def test_stop_is_visible_to_json_waiter(manager, monkeypatch):
    async def scenario():
        entered = asyncio.Event()
        async def events(*args, **kwargs):
            entered.set()
            await asyncio.Event().wait()
            yield "result", "unreachable"
        monkeypatch.setattr(runtime, "agent_event_stream", events)
        run = manager.start("sid", {})
        try:
            await entered.wait()
            assert await manager.stop_and_wait("sid")
            with pytest.raises(RunStopped):
                await manager.wait_result(run)
            assert [e["type"] for e in run.events] == ["stopped"]
        finally:
            await manager.aclose()
    asyncio.run(scenario())


@pytest.mark.parametrize("kind, payload, reply", [
    ("approval", {"pending": True}, "任务已暂停，等待操作审批。"),
    ("clarification", {"pending": True, "clarifications": [{"question": "哪个文件？"}]}, "等待你补充信息：哪个文件？"),
])
def test_suspended_runs_reject_new_messages_and_resume(manager, kind, payload, reply):
    async def scenario():
        async def suspend():
            yield kind, payload
        async def finish():
            yield "result", "恢复完成"
        try:
            run = manager.start("sid", {}, event_stream=suspend)
            assert await manager.wait_result(run) == reply
            assert run.suspended
            manager._expire(run)
            assert manager.current("sid") is run
            with pytest.raises(RunConflict):
                manager.start("sid", {})
            resumed = manager.start("sid", Command(resume={}), event_stream=finish)
            assert await manager.wait_result(resumed) == "恢复完成"
        finally:
            await manager.aclose()
    asyncio.run(scenario())


def test_failed_run_keeps_original_exception_for_http_mapping(manager):
    from app.agent.providers import ProviderNotConfigured
    async def scenario():
        async def events():
            raise ProviderNotConfigured("请先配置模型")
            yield
        try:
            run = manager.start("sid", {}, event_stream=events)
            with pytest.raises(ProviderNotConfigured):
                await manager.wait_result(run)
            assert run.status == "error" and run.events[-1]["type"] == "error"
        finally:
            await manager.aclose()
    asyncio.run(scenario())


def test_replay_is_a_snapshot_while_new_tokens_arrive(manager):
    async def scenario():
        run = SessionRun(session_id="sid", events=[{"type": "token", "content": "A"}])
        manager._runs["sid"] = run
        subscription = manager.subscribe("sid")
        try:
            first = await anext(subscription)
            manager._publish(run, {"type": "token", "content": "B"})
            assert first["content"] == "A"
            assert (await anext(subscription))["content"] == "B"
            assert run.events[0]["content"] == "AB"
        finally:
            await subscription.aclose()
            await manager.aclose()
    asyncio.run(scenario())


def test_finished_run_expires_without_being_queried(manager, monkeypatch):
    from app.agent import runner
    monkeypatch.setattr(runner, "KEEP_SECONDS", 0)
    async def scenario():
        async def events():
            yield "result", "done"
        run = manager.start("sid", {}, event_stream=events)
        await run.task
        # A zero-delay expiry runs on the following event-loop iteration.
        await asyncio.sleep(0)
        assert "sid" not in manager._runs
    asyncio.run(scenario())


def test_full_subscriber_queue_disconnects_without_stopping_run(manager):
    async def scenario():
        queue = asyncio.Queue(maxsize=2)
        queue.put_nowait({"type": "token", "content": "old"})
        queue.put_nowait({"type": "token", "content": "old"})
        run = SessionRun(session_id="sid", subscribers={queue})
        manager._publish(run, {"type": "token", "content": "new"})
        assert run.running and not run.subscribers
        assert (await queue.get())["type"] == "error"
        assert await queue.get() is None
        assert run.events == [{"type": "token", "content": "new"}]
    asyncio.run(scenario())


def test_preflight_rechecks_after_pending_lookup(manager, monkeypatch):
    async def scenario():
        async def pending(*args, **kwargs):
            manager._runs["sid"] = SessionRun(session_id="sid")
            return {"pending": False}
        monkeypatch.setattr(service, "read_pending_approval", pending)
        with pytest.raises(HTTPException) as error:
            await service.prepare_chat(ChatRequest(message="hi", session_id="sid"), SimpleNamespace(id="uid"), None)
        assert error.value.status_code == 409
    asyncio.run(scenario())


def test_stop_before_first_task_step_releases_session(manager):
    async def scenario():
        run = manager.start("sid", {})
        assert manager.stop("sid")
        with pytest.raises(RunStopped):
            await manager.wait_result(run)
        assert run.status == "stopped" and run.finished_at is not None
        assert not manager.is_running("sid")
        await manager.aclose()
    asyncio.run(scenario())


def test_subscription_stays_with_its_run_when_a_new_round_starts(manager):
    async def scenario():
        async def first():
            yield "result", "first"
        async def second():
            yield "result", "second"
        try:
            old = manager.start("sid", {}, event_stream=first)
            await old.task
            new = manager.start("sid", {}, event_stream=second)
            await new.task
            replay = [event async for event in manager.subscribe("sid", run=old)]
            assert replay == [{"type": "result", "content": "first"}]
        finally:
            await manager.aclose()
    asyncio.run(scenario())


def test_run_finishing_during_replay_keeps_queued_final_events(manager):
    async def scenario():
        run = SessionRun(session_id="sid", events=[{"type": "token", "content": "A"}])
        manager._runs["sid"] = run
        subscription = manager.subscribe("sid")
        assert (await anext(subscription))["content"] == "A"
        manager._publish(run, {"type": "token", "content": "B"})
        manager._publish(run, {"type": "result", "content": "AB"})
        run.status = "done"
        manager._close(run)
        remaining = [event async for event in subscription]
        assert remaining == [
            {"type": "token", "content": "B"},
            {"type": "result", "content": "AB"},
        ]
        await manager.aclose()
    asyncio.run(scenario())


@pytest.mark.parametrize("fail_request", [False, True])
def test_shutdown_stops_runs_before_closing_storage(monkeypatch, fail_request):
    from app import main
    from app.agent import context, runner
    from app.db import bootstrap
    order = []
    monkeypatch.setattr(main, "_run_migrations", lambda: None)
    monkeypatch.setattr(bootstrap, "run_bootstrap", lambda: None)
    monkeypatch.setattr(runner.run_manager, "aclose", AsyncMock(side_effect=lambda: order.append("runs")))
    monkeypatch.setattr(context, "aclose", AsyncMock(side_effect=lambda: order.append("storage")))

    async def scenario():
        async with main.lifespan(main.app):
            if fail_request:
                raise ValueError("application failed")
    if fail_request:
        with pytest.raises(ValueError):
            asyncio.run(scenario())
    else:
        asyncio.run(scenario())
    assert order == ["runs", "storage"]
