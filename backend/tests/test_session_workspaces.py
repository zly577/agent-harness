"""Workspace membership and ownership without a live database or model."""
from types import SimpleNamespace
import pytest
from fastapi import HTTPException
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool
from app.db.models import Session as DbSession, Workspace
from app.api.routes import sessions
from app.agent import runtime

@pytest.fixture
def database(monkeypatch, tmp_path):
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
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

@pytest.mark.skipif(__import__("os").name != "nt", reason="Native picker requires Windows")
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


@pytest.fixture
def browser_roots(monkeypatch):
    from app.agent import opensandbox_backend
    monkeypatch.setattr(opensandbox_backend, "machine_mounts", lambda: [])


def test_browser_workspace_creation_and_isolation(database, tmp_path, browser_roots):
    user = SimpleNamespace(id="a" * 32)
    roots = sessions.workspace_directories(user=user)
    base = roots["entries"][0]["directory"]
    from pathlib import Path
    project = Path(base) / "中文项目"
    request = sessions.CreateWorkspaceRequest(directory=str(project), create=True)
    first = sessions.create_workspace(request, user, database)
    assert project.is_dir()
    assert sessions.create_workspace(request, user, database)["id"] == first["id"]
    listing = sessions.workspace_directories(base, user)
    assert listing["parent"] is None
    assert listing["entries"] == [{"name": "中文项目", "directory": str(project)}]
    chat = sessions.create_session(sessions.CreateSessionRequest(workspace_id=first["id"]), user, database)
    assert runtime.session_workspace(chat["id"], user.id) == project
    other = SimpleNamespace(id="b" * 32)
    with pytest.raises(HTTPException) as error:
        sessions.create_workspace(request, other, database)
    assert error.value.status_code == 403
    with pytest.raises(HTTPException) as error:
        sessions.workspace_directories(str(tmp_path), user)
    assert error.value.status_code == 403
    with pytest.raises(HTTPException) as error:
        sessions.create_workspace(sessions.CreateWorkspaceRequest(directory=str(project / "missing")), user, database)
    assert error.value.status_code == 422


def test_browser_mounted_directory(database, tmp_path, monkeypatch):
    from app.agent import opensandbox_backend
    mount = tmp_path / "disk"
    project = mount / "project"
    project.mkdir(parents=True)
    monkeypatch.setattr(opensandbox_backend, "machine_mounts", lambda: [("d", mount)])
    user = SimpleNamespace(id="a" * 32)
    roots = sessions.workspace_directories(user=user)
    assert roots["entries"][1] == {"name": "d", "directory": str(mount)}
    assert sessions.workspace_directories(str(project), user)["parent"] == str(mount)
    result = sessions.create_workspace(sessions.CreateWorkspaceRequest(directory=str(project)), user, database)
    assert result["directory"] == str(project)


def test_browser_rejects_symlink_escape(database, tmp_path, browser_roots):
    user = SimpleNamespace(id="a" * 32)
    from app.core.workspace import ensure_workspace
    outside = tmp_path / "outside"
    outside.mkdir()
    link = ensure_workspace(user.id) / "escape"
    try:
        link.symlink_to(outside, target_is_directory=True)
    except OSError:
        pytest.skip("Directory symlinks unavailable")
    with pytest.raises(HTTPException) as error:
        sessions.create_workspace(sessions.CreateWorkspaceRequest(directory=str(link / "new"), create=True), user, database)
    assert error.value.status_code == 403
    assert not (outside / "new").exists()


@pytest.mark.skipif(__import__("os").name == "nt", reason="Docker protocol path")
def test_native_picker_ticket_result_and_ownership(database, tmp_path, browser_roots, monkeypatch):
    import asyncio
    from app.core.config import settings
    monkeypatch.setattr(settings, "workspace_open_protocol", True)
    monkeypatch.setattr(sessions, "_picker_requests", {})
    user = SimpleNamespace(id="a" * 32)
    other = SimpleNamespace(id="b" * 32)
    request = asyncio.run(sessions.pick_workspace_directory(user, database))
    ticket = request["ticket"]
    assert len(ticket) == 64
    assert request["open_url"] == f"agent-harness-workspace://pick/{ticket}"
    assert sessions.workspace_picker_ready(ticket) == {"ok": True}
    assert sessions.poll_workspace_picker(ticket, user, database) == {"pending": True}
    with pytest.raises(HTTPException) as error:
        sessions.poll_workspace_picker(ticket, other, database)
    assert error.value.status_code == 404
    selected = tmp_path / user.id / "project"
    selected.mkdir(parents=True)
    assert sessions.finish_workspace_picker(ticket, sessions.PickerResultRequest(directory=str(selected))) == {"ok": True}
    first = sessions.poll_workspace_picker(ticket, user, database)
    assert first["directory"] == str(selected)
    assert sessions.poll_workspace_picker(ticket, user, database) == first
    assert database.query(Workspace).count() == 1
    with pytest.raises(HTTPException) as error:
        sessions.finish_workspace_picker(ticket, sessions.PickerResultRequest(cancelled=True))
    assert error.value.status_code == 409


@pytest.mark.skipif(__import__("os").name == "nt", reason="Docker protocol path")
def test_native_picker_cancel_expiry_and_path_validation(database, tmp_path, browser_roots, monkeypatch):
    import asyncio
    from app.core.config import settings
    monkeypatch.setattr(settings, "workspace_open_protocol", True)
    monkeypatch.setattr(sessions, "_picker_requests", {})
    user = SimpleNamespace(id="a" * 32)
    def start():
        return asyncio.run(sessions.pick_workspace_directory(user, database))["ticket"]
    ticket = start()
    sessions.finish_workspace_picker(ticket, sessions.PickerResultRequest(cancelled=True))
    assert sessions.poll_workspace_picker(ticket, user, database) == {"cancelled": True}
    ticket = start()
    sessions._picker_requests[ticket]["expires"] = 0
    with pytest.raises(HTTPException) as error:
        sessions.workspace_picker_ready(ticket)
    assert error.value.status_code == 404
    ticket = start()
    sessions.finish_workspace_picker(ticket, sessions.PickerResultRequest(directory=str(tmp_path)))
    with pytest.raises(HTTPException) as error:
        sessions.poll_workspace_picker(ticket, user, database)
    assert error.value.status_code == 403
    ticket = start()
    sessions.finish_workspace_picker(ticket, sessions.PickerResultRequest(error="Not mounted"))
    with pytest.raises(HTTPException) as error:
        sessions.poll_workspace_picker(ticket, user, database)
    assert error.value.status_code == 422
    ticket = start()
    start()
    with pytest.raises(HTTPException):
        sessions.workspace_picker_ready(ticket)
    monkeypatch.setattr(settings, "workspace_open_protocol", False)
    assert asyncio.run(sessions.pick_workspace_directory(user, database)) == {"browser_picker": True}
    assert database.query(Workspace).count() == 0


@pytest.mark.skipif(__import__("os").name == "nt", reason="Docker protocol path")
def test_native_callback_actual_app_routing(database, tmp_path, browser_roots, monkeypatch):
    import asyncio
    from fastapi.testclient import TestClient
    from app.main import app
    from app.api.deps import get_current_user
    from app.db.session import get_db
    from app.core.config import settings
    monkeypatch.setattr(settings, "workspace_open_protocol", True)
    monkeypatch.setattr(sessions, "_picker_requests", {})
    monkeypatch.setattr(app, "dependency_overrides", {get_db: lambda: database})
    user = SimpleNamespace(id="a" * 32)
    ticket = asyncio.run(sessions.pick_workspace_directory(user, database))["ticket"]
    selected = tmp_path / user.id / "project"
    selected.mkdir(parents=True)
    client = TestClient(app)
    assert client.get(f"/api/v1/workspaces/picker/{ticket}/ready").status_code == 200
    assert client.post(f"/api/v1/workspaces/picker/{ticket}/result", json={"directory": str(selected)}).status_code == 200
    assert client.get(f"/api/v1/workspaces/picker/{ticket}").status_code == 401
    assert client.post("/api/v1/workspaces", json={"directory": str(selected)}).status_code == 401
    assert client.get("/api/v1/workspaces/picker/unknown/ready").status_code == 404
    app.dependency_overrides[get_current_user] = lambda: user
    response = client.get(f"/api/v1/workspaces/picker/{ticket}")
    assert response.status_code == 200
    assert response.json()["directory"] == str(selected)
    client.close()


def test_file_panel_and_open_directory_follow_workspace(database, tmp_path, monkeypatch):
    from fastapi.testclient import TestClient
    from app.main import app
    from app.api.deps import get_current_user
    from app.db.session import get_db
    from app.api.routes import files
    from app.core.config import settings
    monkeypatch.setattr(settings, "workspace_open_protocol", True)
    monkeypatch.setattr(settings, "sandbox_enabled", False)
    monkeypatch.setattr(settings, "harness_machine_access", False)
    monkeypatch.setattr(files, "os", SimpleNamespace(name="posix"))
    monkeypatch.setattr(files, "_open_requests", {})
    user = SimpleNamespace(id="a" * 32)
    monkeypatch.setattr(app, "dependency_overrides", {get_db: lambda: database, get_current_user: lambda: user})
    selected = tmp_path / "中文项目"
    selected.mkdir()
    (selected / "selected.txt").write_text("selected")
    database.add(Workspace(id="d" * 32, user_id=user.id, name=selected.name, directory=str(selected)))
    database.commit()
    session = sessions.create_session(sessions.CreateSessionRequest(workspace_id="d" * 32), user, database)
    client = TestClient(app)
    for params in ({"workspace_id": "d" * 32}, {"session_id": session["id"]}, {"session_id": session["id"], "workspace_id": "legacy"}):
        listing = client.get("/api/v1/files", params={"path": "/workspace", **params})
        assert listing.status_code == 200
        assert any(entry["path"].endswith("selected.txt") for entry in listing.json()["entries"])
        response = client.post("/api/v1/files/open-workspace", params=params)
        assert response.status_code == 200
        ticket = response.json()["open_url"].rsplit("/", 1)[1]
        assert client.get(f"/api/v1/files/open-workspace/{ticket}").json() == {"directory": str(selected)}
        assert client.get(f"/api/v1/files/open-workspace/{ticket}").status_code == 404
    response = client.post("/api/v1/files/open-workspace", params={"workspace_id": "legacy"})
    ticket = response.json()["open_url"].rsplit("/", 1)[1]
    assert client.get(f"/api/v1/files/open-workspace/{ticket}").json() == {"directory": str(tmp_path / user.id)}
    response = client.post("/api/v1/files/open-workspace", params={"workspace_id": "d" * 32})
    ticket = response.json()["open_url"].rsplit("/", 1)[1]
    files._open_requests[ticket]["expires"] = 0
    assert client.get(f"/api/v1/files/open-workspace/{ticket}").status_code == 404
    app.dependency_overrides[get_current_user] = lambda: SimpleNamespace(id="b" * 32)
    assert client.post("/api/v1/files/open-workspace", params={"workspace_id": "d" * 32}).status_code == 404
    assert client.post("/api/v1/files/open-workspace", params={"session_id": session["id"]}).status_code == 404
    app.dependency_overrides[get_current_user] = lambda: user
    (selected / "selected.txt").unlink()
    selected.rmdir()
    assert client.post("/api/v1/files/open-workspace", params={"workspace_id": "d" * 32}).status_code == 409
    assert not selected.exists()
    client.close()
