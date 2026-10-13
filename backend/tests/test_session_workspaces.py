"""Workspace membership and ownership without a live database or model."""
from types import SimpleNamespace
import pytest
from fastapi import HTTPException
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from app.db.models import Session as DbSession, Workspace
from app.api.routes import sessions
from app.agent import runtime

@pytest.fixture
def database(monkeypatch, tmp_path):
    engine = create_engine("sqlite://")
    DbSession.__table__.create(engine)
    Workspace.__table__.create(engine)
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    monkeypatch.setattr(runtime, "SessionLocal", factory)
    monkeypatch.setenv("HARNESS_WORKSPACE", str(tmp_path))
    with factory() as db:
        yield db
    engine.dispose()

def test_new_and_shared_workspaces(database, tmp_path):
    user = SimpleNamespace(id="a" * 32)
    selected = tmp_path / "my-project"
    selected.mkdir()
    database.add(Workspace(id="d" * 32, user_id=user.id, name=selected.name, directory=str(selected)))
    database.commit()
    first = sessions.create_session(sessions.CreateSessionRequest(workspace_id="d" * 32), user, database)
    shared = sessions.create_session(sessions.CreateSessionRequest(workspace_id=first["workspace_id"]), user, database)
    separate = sessions.create_session(sessions.CreateSessionRequest(), user, database)
    assert separate["workspace_id"] == "legacy"
    root = runtime.session_workspace(first["id"], user.id)
    assert root == selected
    (root / "shared.txt").write_text("hello")
    assert (runtime.session_workspace(shared["id"], user.id) / "shared.txt").read_text() == "hello"
    assert not (runtime.session_workspace(separate["id"], user.id) / "shared.txt").exists()
    with pytest.raises(HTTPException) as error:
        sessions.create_session(sessions.CreateSessionRequest(workspace_id=first["workspace_id"]), SimpleNamespace(id="b" * 32), database)
    assert error.value.status_code == 404


def test_legacy_workspace(database):
    user = SimpleNamespace(id="a" * 32)
    database.add(DbSession(id="c" * 32, user_id=user.id, title="old", status="active"))
    database.commit()
    shared = sessions.create_session(sessions.CreateSessionRequest(workspace_id="legacy"), user, database)
    assert shared["workspace_id"] == "legacy"
    assert runtime.session_workspace(shared["id"], user.id) == runtime.workspace_for(user.id)

def test_delete_workspace_retains_files(database, tmp_path, monkeypatch):
    import asyncio
    user = SimpleNamespace(id="a" * 32)
    selected = tmp_path / "project"
    selected.mkdir()
    (selected / "keep.txt").write_text("keep")
    database.add(Workspace(id="d" * 32, user_id=user.id, name="project", directory=str(selected)))
    database.commit()
    session = sessions.create_session(sessions.CreateSessionRequest(workspace_id="d" * 32), user, database)
    async def stop(session_id):
        return False
    monkeypatch.setattr(sessions.run_manager, "stop_and_wait", stop)
    asyncio.run(sessions.delete_workspace("d" * 32, user, database))
    assert database.get(Workspace, "d" * 32) is None
    assert database.get(DbSession, session["id"]) is None
    assert (selected / "keep.txt").read_text() == "keep"
    assert sessions.list_workspaces(user, database)[0]["name"] == "默认工作区"

def test_directory_picker_cancel_and_name(database, tmp_path, monkeypatch):
    import asyncio
    user = SimpleNamespace(id="a" * 32)
    selected = tmp_path / "中文项目"
    selected.mkdir()
    response = [str(selected).encode("utf-8")]
    import subprocess
    def launch(args, **kwargs):
        assert "-STA" in args
        return subprocess.CompletedProcess(args, 0, stdout=response[0], stderr=b"")
    monkeypatch.setattr(subprocess, "run", launch)
    first = asyncio.run(sessions.pick_workspace_directory(user, database))
    assert first["name"] == "中文项目"
    assert first["directory"] == str(selected.resolve())
    second = asyncio.run(sessions.pick_workspace_directory(user, database))
    assert first["id"] == second["id"]
    response[0] = b""
    assert asyncio.run(sessions.pick_workspace_directory(user, database)) == {"cancelled": True}
    assert database.query(Workspace).count() == 1

def test_draft_files_and_first_message_use_selected_directory(database, tmp_path):
    from app.api.routes.files import _workspace_root
    user = SimpleNamespace(id="a" * 32)
    selected = tmp_path / "draft-project"
    selected.mkdir()
    database.add(Workspace(id="d" * 32, user_id=user.id, name=selected.name, directory=str(selected)))
    database.commit()
    assert _workspace_root(user, "d" * 32, database) == selected
    session_id = runtime.create_session(user.id, "第一条消息", workspace_id="d" * 32)
    assert runtime.session_workspace(session_id, user.id) == selected
    with pytest.raises(HTTPException) as error:
        _workspace_root(SimpleNamespace(id="b" * 32), "d" * 32, database)
    assert error.value.status_code == 404
