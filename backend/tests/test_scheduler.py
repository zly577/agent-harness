import asyncio
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, time, timedelta
from uuid import uuid4

import pytest
from sqlalchemy import delete, select

from app.agent import runtime
from app.agent.runner import run_manager
from app.db.models import ScheduledRun, ScheduledTask, Workspace
from app.db.models import Session as DbSession
from app.db.session import SessionLocal
from app.services import scheduler
from app.services.schedule_rules import utcnow


@pytest.fixture
def make_task(auth_user):
    task_ids = []

    def create(*, due=None, frequency="daily", **overrides):
        task_id = uuid4().hex
        now = utcnow()
        fields = {
            "id": task_id, "user_id": auth_user["id"], "title": "Scheduled test", "prompt": "Generate a report",
            "timezone": "UTC", "frequency": frequency, "time_of_day": time(9), "weekdays": [],
            "once_at": due if frequency == "once" else None, "next_run_at": due,
            "created_at": now, "updated_at": now, "enabled": True,
        }
        with SessionLocal.begin() as db:
            db.add(ScheduledTask(**(fields | overrides)))
        task_ids.append(task_id)
        return task_id

    yield create
    with SessionLocal.begin() as db:
        session_ids = list(db.scalars(
            select(ScheduledRun.session_id).where(
                ScheduledRun.task_id.in_(task_ids), ScheduledRun.session_id.is_not(None)
            )
        ))
        db.execute(delete(ScheduledRun).where(ScheduledRun.task_id.in_(task_ids)))
        db.execute(delete(ScheduledTask).where(ScheduledTask.id.in_(task_ids)))
        db.execute(delete(DbSession).where(DbSession.id.in_(session_ids)))


def _runs(task_id):
    with SessionLocal() as db:
        return list(db.scalars(
            select(ScheduledRun).where(ScheduledRun.task_id == task_id)
            .order_by(ScheduledRun.scheduled_for, ScheduledRun.created_at)
        ))


async def _wait_for(task_id, status):
    async with asyncio.timeout(5):
        while True:
            matches = [run for run in await asyncio.to_thread(_runs, task_id) if run.status == status]
            if matches:
                return matches[-1]
            await asyncio.sleep(.01)


def test_concurrent_scans_record_one_recent_occurrence_and_advance(make_task):
    now = datetime(2026, 10, 15, 9, 1)
    task_id = make_task(due=datetime(2026, 10, 12, 9))
    with ThreadPoolExecutor(max_workers=2) as pool:
        list(pool.map(scheduler.collect_due, [now, now]))

    assert [(run.scheduled_for, run.status) for run in _runs(task_id)] == [
        (datetime(2026, 10, 12, 9), "missed"), (datetime(2026, 10, 15, 9), "queued"),
    ]
    with SessionLocal() as db:
        assert db.get(ScheduledTask, task_id).next_run_at == datetime(2026, 10, 16, 9)


def test_no_overlap_and_expired_one_off(make_task):
    now = datetime(2026, 10, 15, 9, 0, 30)
    task_id = make_task(due=now.replace(second=0))
    missed_id = make_task(due=now - timedelta(minutes=3), frequency="once")
    with SessionLocal.begin() as db:
        db.add(ScheduledRun(
            id=uuid4().hex, task_id=task_id, status="awaiting_approval", trigger_type="manual", created_at=now,
        ))

    scheduler.collect_due(now)

    assert {run.status for run in _runs(task_id)} == {"skipped", "awaiting_approval"}
    assert [run.status for run in _runs(missed_id)] == ["missed"]
    with SessionLocal() as db:
        assert db.get(ScheduledTask, missed_id).next_run_at is None


def test_missing_workspace_pauses_schedule(make_task):
    now = utcnow()
    task_id = make_task(due=now, workspace_id=uuid4().hex)
    scheduler.collect_due(now)
    queued = _runs(task_id)[0]

    assert scheduler._claim(queued.id, task_id, now) is None
    row = _runs(task_id)[0]
    assert row.status == "failed" and "工作区" in row.reason and row.session_id is None
    with SessionLocal() as db:
        assert not db.get(ScheduledTask, task_id).enabled


def test_scheduled_run_can_expire_while_queued(make_task):
    now = utcnow()
    task_id = make_task(due=now, frequency="once")
    scheduler.collect_due(now)
    queued = _runs(task_id)[0]

    assert scheduler._claim(queued.id, task_id, now + timedelta(minutes=3)) is None
    row = _runs(task_id)[0]
    assert row.status == "missed" and row.session_id is None and row.finished_at is not None


def test_shutdown_during_claim_never_starts_agent(make_task, monkeypatch):
    now = utcnow()
    task_id = make_task(due=now, frequency="once")
    scheduler.collect_due(now)
    queued = _runs(task_id)[0]
    chat = scheduler._claim(queued.id, task_id, now)

    def unexpected_start(*args, **kwargs):
        pytest.fail("an Agent must not start during shutdown")

    monkeypatch.setattr(scheduler, "start_chat", unexpected_start)

    async def scenario():
        service = scheduler.Scheduler()
        await service.stop_dispatching()
        service._start_claimed(chat)
        await service.close()

    asyncio.run(scenario())
    assert _runs(task_id)[0].status == "interrupted"


@pytest.mark.parametrize("retry_resume", [False, True])
def test_background_dispatch_and_resume_update_same_history(make_task, auth_user, monkeypatch, retry_resume):
    task_id = make_task(due=utcnow(), frequency="once")
    calls = []

    async def events(session_id, agent_input, user_id=None):
        calls.append((session_id, user_id))
        if len(calls) == 1:
            assert agent_input == {"messages": [{"role": "user", "content": "Generate a report"}]}
            yield "clarification", {"clarifications": [{"id": "q1", "question": "Which project?"}]}
        elif retry_resume and len(calls) == 2:
            raise RuntimeError("Temporary model error")
        else:
            yield "result", "Report ready."

    async def pending_during_retry(session_id, owner_id):
        retry = run_manager.start(session_id, runtime.resume_input({"q1": "Project"}), user_id=owner_id)
        await retry.task
        return None

    monkeypatch.setattr(runtime, "agent_event_stream", events)
    if retry_resume:
        monkeypatch.setattr(scheduler, "_pending_status", pending_during_retry)
    monkeypatch.setattr(scheduler, "_POLL_SECONDS", .01)

    async def scenario():
        service = scheduler.Scheduler()
        await service.start()
        try:
            pending = await _wait_for(task_id, "awaiting_input")
            assert pending.started_at is not None and pending.finished_at is None
            with SessionLocal() as db:
                session = db.get(DbSession, pending.session_id)
                assert session.title == "Scheduled test" and session.user_id == auth_user["id"]
            run_manager.start(pending.session_id, runtime.resume_input({"q1": "Project"}), user_id=auth_user["id"])
            done = await _wait_for(task_id, "succeeded")
            assert done.id == pending.id and done.finished_at is not None
            assert calls == [(pending.session_id, auth_user["id"])] * (3 if retry_resume else 2)
        finally:
            await service.stop_dispatching()
            await run_manager.aclose()
            await service.close()

    asyncio.run(scenario())


def test_restart_recovers_checkpoint_and_interrupts_uncertain_run(make_task, auth_user, monkeypatch):
    pending_task = make_task()
    interrupted_task = make_task()
    pending_id, interrupted_id = uuid4().hex, uuid4().hex
    now = utcnow()
    with SessionLocal.begin() as db:
        for task_id, session_id in [(pending_task, pending_id), (interrupted_task, interrupted_id)]:
            db.add(DbSession(id=session_id, user_id=auth_user["id"]))
            db.flush()
            db.add(ScheduledRun(
                id=uuid4().hex, task_id=task_id, status="running", trigger_type="manual", created_at=now,
                session_id=session_id, started_at=now,
            ))

    async def checkpoint(session_id, owner_id):
        return "awaiting_approval" if session_id == pending_id else None

    monkeypatch.setattr(scheduler, "_pending_status", checkpoint)

    async def scenario():
        service = scheduler.Scheduler()
        await service.start()
        try:
            assert _runs(pending_task)[0].status == "awaiting_approval"
            assert _runs(interrupted_task)[0].status == "interrupted"
            assert run_manager.current(interrupted_id) is None
        finally:
            await service.stop_dispatching()
            await run_manager.aclose()
            await service.close()

    asyncio.run(scenario())


def test_delete_workspace_unlinks_execution_and_pauses_task(make_task, auth_client, auth_user, tmp_path):
    workspace_id, session_id = uuid4().hex, uuid4().hex
    with SessionLocal.begin() as db:
        db.add(Workspace(id=workspace_id, user_id=auth_user["id"], name="Temporary", directory=str(tmp_path)))
        db.add(DbSession(id=session_id, user_id=auth_user["id"], workspace_id=workspace_id))
    task_id = make_task(workspace_id=workspace_id, due=utcnow())
    with SessionLocal.begin() as db:
        db.add(ScheduledRun(
            id=uuid4().hex, task_id=task_id, session_id=session_id, status="awaiting_approval",
            trigger_type="manual", created_at=utcnow(),
        ))

    response = auth_client.delete(f"/api/v1/workspaces/{workspace_id}")

    assert response.status_code == 200, response.text
    row = _runs(task_id)[0]
    assert row.status == "stopped" and row.session_id is None
    with SessionLocal() as db:
        task = db.get(ScheduledTask, task_id)
        assert not task.enabled and task.next_run_at is None and task.workspace_id == workspace_id
