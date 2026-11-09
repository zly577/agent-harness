from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, time
from uuid import uuid4

import pytest
from sqlalchemy import delete, select

from app.api.routes import schedules
from app.db.models import LlmProvider, ScheduledRun, ScheduledTask, Workspace
from app.db.session import SessionLocal
from app.services.schedule_rules import next_occurrence
from tests.helpers import make_auth_client

PATH = "/api/v1/schedules"
DAILY = {"title": "晨报", "prompt": "读取工作区并汇总进展", "timezone": "Asia/Shanghai",
         "frequency": "daily", "time_of_day": "09:00"}


@pytest.fixture
def auth_client():
    client, user = make_auth_client(raise_server_exceptions=False)
    yield client
    with SessionLocal.begin() as db:
        tasks = ScheduledTask.user_id == user["id"]
        db.execute(delete(ScheduledRun).where(ScheduledRun.task_id.in_(select(ScheduledTask.id).where(tasks))))
        db.execute(delete(ScheduledTask).where(tasks))


def test_recurring_uses_local_time_and_skips_dst_gaps():
    task = ScheduledTask(frequency="daily", timezone="America/New_York", time_of_day=time(2, 30), weekdays=[])
    assert next_occurrence(task, datetime(2027, 3, 13, 7, 29)) == datetime(2027, 3, 13, 7, 30)
    assert next_occurrence(task, datetime(2027, 3, 13, 7, 30)) == datetime(2027, 3, 15, 6, 30)

    task.frequency = "weekly"
    task.weekdays = [7]
    task.time_of_day = time(1, 30)
    assert next_occurrence(task, datetime(2027, 11, 7, 4)) == datetime(2027, 11, 7, 5, 30)
    assert next_occurrence(task, datetime(2027, 11, 7, 5, 30)) == datetime(2027, 11, 14, 6, 30)


def test_schedule_create_pause_edit_and_resume(auth_client, monkeypatch):
    now = datetime(2027, 1, 1)
    monkeypatch.setattr(schedules, "utcnow", lambda: now)
    result = auth_client.post(PATH, json=DAILY)
    assert result.status_code == 201, result.text
    task = result.json()
    assert task["next_run_at"] == "2027-01-01T01:00:00Z"
    assert task["time_of_day"] == "09:00"
    path = f"{PATH}/{task['id']}"

    paused = auth_client.post(f"{path}/pause").json()
    assert paused["enabled"] is False and paused["next_run_at"] is None
    changed = auth_client.patch(path, json={"frequency": "weekly", "weekdays": [5, 1, 5]})
    assert changed.status_code == 200, changed.text
    assert changed.json()["weekdays"] == [1, 5]
    assert changed.json()["next_run_at"] is None

    now = datetime(2027, 1, 1, 2)
    resumed = auth_client.post(f"{path}/resume").json()
    assert resumed["enabled"] is True
    assert resumed["next_run_at"] == "2027-01-04T01:00:00Z"


def test_one_shot_validates_gap_and_preserves_past_time_on_title_edit(auth_client, monkeypatch):
    now = datetime(2027, 3, 1)
    monkeypatch.setattr(schedules, "utcnow", lambda: now)
    once = {**DAILY, "frequency": "once", "timezone": "America/New_York", "once_at": "2027-03-14T02:30"}
    assert auth_client.post(PATH, json=once).status_code == 422
    assert auth_client.post(PATH, json={**once, "once_at": "2027-03-14T03:30Z"}).status_code == 422
    assert auth_client.post(PATH, json={**DAILY, "timezone": "Mars/Olympus_Mons"}).status_code == 422
    assert auth_client.post(PATH, json={**DAILY, "frequency": "weekly"}).status_code == 422
    response = auth_client.post(PATH, json={**once, "once_at": "2027-03-14T03:30"})
    assert response.status_code == 201, response.text
    task = response.json()
    assert task["once_at"] == task["next_run_at"] == "2027-03-14T07:30:00Z"
    assert task["time_of_day"] is None
    path = f"{PATH}/{task['id']}"
    assert auth_client.post(f"{path}/pause").status_code == 200

    now = datetime(2027, 3, 14, 8)
    edited = auth_client.patch(path, json={"title": "改名"})
    assert edited.status_code == 200, edited.text
    assert edited.json()["once_at"] == task["once_at"]
    assert auth_client.patch(path, json={"title": None}).status_code == 422
    assert auth_client.post(f"{path}/resume").status_code == 409


def test_concurrent_manual_trigger_serializes_per_task(auth_client):
    task = auth_client.post(PATH, json=DAILY).json()
    path = f"{PATH}/{task['id']}"
    assert auth_client.post(f"{path}/pause").status_code == 200
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda _: auth_client.post(f"{path}/run-now"), range(2)))
    assert all(response.status_code == 202 for response in results), [response.text for response in results]
    runs = auth_client.get(f"{path}/runs").json()
    assert [run["status"] for run in runs] == ["skipped", "queued"]
    assert runs[0]["reason"] and runs[0]["finished_at"].endswith("Z")
    assert runs[1]["trigger_type"] == "manual" and runs[1]["scheduled_for"] is None
    listed = next(row for row in auth_client.get(PATH).json() if row["id"] == task["id"])
    assert listed["latest_run"]["id"] == runs[0]["id"]


def test_schedule_actions_and_history_are_owner_scoped(auth_client, admin_client, anon_client):
    task = auth_client.post(PATH, json=DAILY).json()
    path = f"{PATH}/{task['id']}"
    assert anon_client.get(PATH).status_code == 401
    assert all(item["id"] != task["id"] for item in admin_client.get(PATH).json())
    assert admin_client.patch(path, json={"title": "other"}).status_code == 404
    assert admin_client.get(f"{path}/runs").status_code == 404
    for action in ("pause", "resume", "run-now"):
        assert admin_client.post(f"{path}/{action}").status_code == 404


def test_schedule_cannot_bind_another_owners_workspace_or_provider(auth_client, admin_user, tmp_path):
    workspace_id, provider_id = uuid4().hex, uuid4().hex
    with SessionLocal.begin() as db:
        db.add(Workspace(id=workspace_id, user_id=admin_user["id"], name="other", directory=str(tmp_path)))
        db.add(LlmProvider(id=provider_id, user_id=admin_user["id"], name="other"))
    try:
        assert auth_client.post(PATH, json={**DAILY, "workspace_id": workspace_id}).status_code == 404
        assert auth_client.post(PATH, json={**DAILY, "provider_id": provider_id}).status_code == 404
    finally:
        with SessionLocal.begin() as db:
            db.execute(delete(Workspace).where(Workspace.id == workspace_id))
            db.execute(delete(LlmProvider).where(LlmProvider.id == provider_id))


def test_unavailable_directory_requires_a_replacement(auth_client, tmp_path):
    user_id = auth_client.get("/api/v1/auth/me").json()["id"]
    workspace_id = uuid4().hex
    directory = tmp_path / "schedule-workspace"
    directory.mkdir()
    with SessionLocal.begin() as db:
        db.add(Workspace(id=workspace_id, user_id=user_id, name="scheduled", directory=str(directory)))
    try:
        response = auth_client.post(PATH, json={**DAILY, "workspace_id": workspace_id})
        assert response.status_code == 201, response.text
        path = f"{PATH}/{response.json()['id']}"
        directory.rmdir()
        assert auth_client.post(f"{path}/pause").status_code == 200
        assert auth_client.post(f"{path}/resume").status_code == 409
        assert auth_client.post(f"{path}/run-now").status_code == 409
        assert auth_client.patch(path, json={"title": "仍绑定已移除目录"}).status_code == 409
        assert auth_client.patch(path, json={"workspace_id": None}).status_code == 200
        assert auth_client.post(f"{path}/resume").status_code == 200
    finally:
        with SessionLocal.begin() as db:
            db.execute(delete(Workspace).where(Workspace.id == workspace_id))
