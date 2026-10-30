import asyncio
import json
import pytest

from deepagents.backends import FilesystemBackend

from app.agent.memory_tools import build_memory_tools
from app.agent.workspace_memory import WorkspaceMemoryRepairMiddleware


def test_detail_memory_is_indexed_without_loading_body_and_core_is_bounded(tmp_path):
    tool = build_memory_tools(tmp_path)[0]
    result = json.loads(tool.invoke({"key": "build", "title": "构建经验", "content": "DETAIL BODY MUST STAY OUT OF PROMPT",
                                    "source": "tool:build-42"}))
    assert result["saved"]
    detail = (tmp_path / ".agent-memory/build.md").read_text(encoding="utf-8")
    assert "tool:build-42" in detail and "更新时间" in detail and "当前用户" in detail
    original = "中文核心偏好\n" + "old details" * 1000
    (tmp_path / "Agent.md").write_text(original, encoding="utf-8")
    middleware = WorkspaceMemoryRepairMiddleware(FilesystemBackend(root_dir=tmp_path, virtual_mode=True), ["/Agent.md"])
    async def run():
        update = await middleware.abefore_agent({}, None)
        content = update["memory_contents"]["/Agent.md"]
        assert "核心偏好" in content and ".agent-memory/build.md" in content
        assert "DETAIL BODY MUST STAY OUT OF PROMPT" not in content
        assert len(content) < 10000 and "仅加载前" in content
        assert (tmp_path / "Agent.md").read_text(encoding="utf-8") == original
    asyncio.run(run())


def test_memory_revision_and_core_capacity_preserve_existing_content(tmp_path):
    tool = build_memory_tools(tmp_path)[0]
    args = {"key": "language", "title": "语言", "content": "请用中文", "source": "user:message-1", "tier": "core"}
    first = json.loads(tool.invoke(args))
    assert first["saved"]
    path = tmp_path / "Agent.md"
    # A user edits the file after the agent's read.
    path.write_text(path.read_text(encoding="utf-8") + "\n用户最新修改", encoding="utf-8")
    conflict = json.loads(tool.invoke({**args, "content": "过期替换", "expected_revision": first["revision"]}))
    assert conflict["error"] == "revision_conflict"
    assert "用户最新修改" in path.read_text(encoding="utf-8")
    full = json.loads(tool.invoke({**args, "content": "x" * 7000, "expected_revision": conflict["revision"]}))
    assert full["error"] == "core_full"
    assert "用户最新修改" in path.read_text(encoding="utf-8")
    assert not (tmp_path / ".agent-memory").exists()


def test_memory_scope_and_symlink_rejection(tmp_path):
    first, second = tmp_path / "first", tmp_path / "second"
    first.mkdir(); second.mkdir()
    args = {"key": "project", "title": "项目", "content": "first-only", "source": "message:1"}
    assert json.loads(build_memory_tools(first)[0].invoke(args))["saved"]
    assert not (second / ".agent-memory").exists()
    target = tmp_path / "external.md"
    target.write_text("outside", encoding="utf-8")
    try:
        (second / "Agent.md").symlink_to(target)
    except OSError:
        return
    rejected = json.loads(build_memory_tools(second)[0].invoke({**args, "tier": "core"}))
    assert "error" in rejected
    assert target.read_text(encoding="utf-8") == "outside"


def test_memory_conflicts_are_reported_as_tool_failures(tmp_path):
    tool = build_memory_tools(tmp_path)[0]
    args = {"key": "project", "title": "约定", "content": "confirmed", "source": "user:1"}
    assert json.loads(tool.invoke(args))["saved"]
    response = tool.invoke({"type": "tool_call", "name": "remember", "id": "memory-conflict", "args": args})
    assert response.status == "error"
    assert json.loads(response.content)["error"] == "revision_conflict"


@pytest.mark.parametrize("mode", ["sandbox", "full"])
def test_memory_route_survives_project_switch_and_isolates_users(tmp_path, monkeypatch, mode):
    from app.agent.prompt import ensure_memory_file, memory_sources
    from app.agent.sandbox import build_main_backend
    from app.core.config import settings

    monkeypatch.setenv("HARNESS_WORKSPACE", str(tmp_path / "users"))
    monkeypatch.setattr(settings, "harness_machine_access", False)
    monkeypatch.setattr(settings, "sandbox_enabled", False)
    first_project, second_project = tmp_path / "project-a", tmp_path / "project-b"
    first_project.mkdir(); second_project.mkdir()
    user_memory = ensure_memory_file("first-user")
    user_memory.write_text("USER PREFERENCES", encoding="utf-8")
    other_memory = ensure_memory_file("second-user")
    other_memory.write_text("OTHER USER PREFERENCES", encoding="utf-8")
    args = {"key": "build", "title": "构建经验", "content": "PROJECT A BUILD NOTES", "source": "project-a:build"}
    saved = json.loads(build_memory_tools(user_memory.parent)[0].invoke(args))
    assert saved["path"] == "/memories/.agent-memory/build.md"

    for project in (first_project, second_project):
        backend = build_main_backend(workspace=project, mode=mode, memory_root=user_memory.parent)
        assert "PROJECT A BUILD NOTES" in backend.read(saved["path"]).file_data["content"]
        matches = backend.glob("/memories/.agent-memory/*.md", path="/memories/.agent-memory")
        assert not matches.error and any(entry["path"] == saved["path"] for entry in matches.matches)
        middleware = WorkspaceMemoryRepairMiddleware(backend, memory_sources("first-user", workspace=project))
        update = asyncio.run(middleware.abefore_agent({}, None))
        content = update["memory_contents"]["/memories/Agent.md"]
        assert "USER PREFERENCES" in content and saved["path"] in content
        assert "PROJECT A BUILD NOTES" not in content
        assert not (project / "Agent.md").exists() and not (project / ".agent-memory").exists()

    other = build_main_backend(workspace=second_project, mode=mode, memory_root=other_memory.parent)
    assert "OTHER USER PREFERENCES" in other.read("/memories/Agent.md").file_data["content"]
    assert other.read(saved["path"]).error
