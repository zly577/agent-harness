import asyncio
import json

from deepagents.backends import FilesystemBackend

from app.agent.memory_tools import build_memory_tools
from app.agent.workspace_memory import WorkspaceMemoryRepairMiddleware


def test_detail_memory_is_indexed_without_loading_body_and_core_is_bounded(tmp_path):
    tool = build_memory_tools(tmp_path)[0]
    result = json.loads(tool.invoke({"key": "build", "title": "构建经验", "content": "DETAIL BODY MUST STAY OUT OF PROMPT",
                                    "source": "tool:build-42"}))
    assert result["saved"]
    detail = (tmp_path / ".agent-memory/build.md").read_text(encoding="utf-8")
    assert "tool:build-42" in detail and "更新时间" in detail and "当前工作区" in detail
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
