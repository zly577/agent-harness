"""个人部署双模式回归：仅操作临时目录，不连接模型、数据库或实际桌面。"""
import asyncio
from types import SimpleNamespace

import pytest

from app.agent import access, mcp, sandbox
from app.core.config import settings


@pytest.fixture()
def desktop(tmp_path, monkeypatch):
    disk = tmp_path / "disk"
    disk.mkdir()
    (disk / "note.txt").write_text("original", encoding="utf-8")
    monkeypatch.setattr(settings, "harness_machine_access", True)
    monkeypatch.setattr(settings, "harness_machine_mounts", f"disk={disk.as_posix()}")
    monkeypatch.setattr(settings, "harness_allow_full_access", True)
    monkeypatch.setattr(settings, "sandbox_enabled", False)
    return disk, tmp_path / "workspace"


def test_default_disk_read_and_workspace_write_without_opensandbox(desktop):
    disk, workspace = desktop
    backend = sandbox.build_main_backend(workspace=str(workspace), mode="sandbox")
    assert backend.read("/mnt/disk/note.txt").file_data["content"] == "original"
    assert backend.write("/workspace/result.txt", "report").error is None
    assert backend.execute("echo default-mode").exit_code != 0
    assert backend.write("/mnt/disk/new.txt", "no").error
    assert backend.edit("/mnt/disk/note.txt", "original", "no").error
    assert backend.delete("/mnt/disk/note.txt").error
    assert backend.upload_files([("/mnt/disk/new.txt", b"no")])[0].error
    assert (disk / "note.txt").read_text(encoding="utf-8") == "original"
    assert not (disk / "new.txt").exists()
    assert (workspace / "result.txt").read_text(encoding="utf-8") == "report"


def test_async_disk_writes_cannot_bypass_default_mode(desktop):
    disk, workspace = desktop
    backend = sandbox.build_main_backend(workspace=str(workspace), mode="sandbox")

    async def check():
        assert (await backend.awrite("/mnt/disk/new.txt", "no")).error
        assert (await backend.aedit("/mnt/disk/note.txt", "original", "no")).error
        assert (await backend.adelete("/mnt/disk/note.txt")).error
        assert (await backend.aupload_files([("/mnt/disk/new.txt", b"no")]))[0].error

    asyncio.run(check())
    assert (disk / "note.txt").read_text(encoding="utf-8") == "original"
    assert not (disk / "new.txt").exists()


def test_full_mode_writes_and_executes_without_opensandbox(desktop):
    disk, workspace = desktop
    backend = sandbox.build_main_backend(workspace=str(workspace), mode="full")
    assert backend.write("/mnt/disk/new.txt", "one").error is None
    assert backend.edit("/mnt/disk/new.txt", "one", "two").error is None
    assert (disk / "new.txt").read_text(encoding="utf-8") == "two"
    result = backend.execute("echo local-shell-ok")
    assert result.exit_code == 0 and "local-shell-ok" in result.output
    assert backend.delete("/mnt/disk/new.txt").error is None
    assert not (disk / "new.txt").exists()


def test_mcp_enabled_with_no_user_tools_makes_no_connections(monkeypatch):
    monkeypatch.setattr(settings, "mcp_enabled", True)
    monkeypatch.setattr(mcp, "_has_adapters", lambda: pytest.fail("Empty config must not initialize MCP clients"))
    assert asyncio.run(mcp.build_mcp_tools([])) == ([], [])


def test_workspace_url_uses_authenticated_user_only(tmp_path, monkeypatch):
    from fastapi.testclient import TestClient
    from app.api.deps import get_current_user
    from app.api.routes import files
    from app.db.models import User
    from app.db.session import get_db
    from app.main import app

    user_id = "a" * 32
    monkeypatch.setenv("HARNESS_WORKSPACE", str(tmp_path))
    monkeypatch.setattr(settings, "workspace_open_protocol", True)
    monkeypatch.setattr(files, "os", SimpleNamespace(name="posix"))
    saved = dict(app.dependency_overrides)
    app.dependency_overrides[get_current_user] = lambda: User(id=user_id, email="desktop@example.com", role="user")
    app.dependency_overrides[get_db] = lambda: None
    try:
        response = TestClient(app).post("/api/v1/files/open-workspace", json={"user_id": "b" * 32, "path": "../../outside"})
        assert response.status_code == 200
        assert response.json()["open_url"] == f"agent-harness-workspace://open/{user_id}"
        assert (tmp_path / user_id).is_dir()
        assert not (tmp_path / ("b" * 32)).exists()
        monkeypatch.setattr(settings, "workspace_open_protocol", False)
        assert TestClient(app).post("/api/v1/files/open-workspace").status_code == 409
    finally:
        app.dependency_overrides.clear()
        app.dependency_overrides.update(saved)
