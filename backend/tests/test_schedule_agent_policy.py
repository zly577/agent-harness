import asyncio
from uuid import uuid4

import pytest
from deepagents.backends.protocol import ExecuteResponse
from langchain_core.language_models.fake_chat_models import FakeMessagesListChatModel
from langchain_core.messages import AIMessage
from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver
from langgraph.store.memory import InMemoryStore

from app.agent import access, approvals, permissions, runtime, sandbox
from app.agent.opensandbox_backend import HybridDefaultBackend, SandboxRegistry
from app.agent.providers import ProviderNotConfigured
from app.agent.runner import RunManager
from app.api.routes.approvals import clear_pending_interrupt
from app.core.config import settings
from app.core.crypto import encrypt_secret
from app.db.models import LlmProvider, ScheduledRun, ScheduledTask, Session, Workspace
from app.db.session import SessionLocal
from app.services.schedule_rules import utcnow


class FakeScheduleModel(FakeMessagesListChatModel):
    def bind_tools(self, tools, **kwargs):
        return self


@pytest.fixture
def scheduled_session(auth_user, tmp_path):
    owner = auth_user["id"]
    session_id, task_id, run_id, workspace_id = (uuid4().hex for _ in range(4))
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    now = utcnow()
    with SessionLocal.begin() as db:
        db.add(Workspace(id=workspace_id, user_id=owner, directory=str(workspace), name="Test workspace"))
        db.add(Session(id=session_id, user_id=owner, workspace_id=workspace_id))
        db.add(ScheduledTask(id=task_id, user_id=owner, title="Test schedule", prompt="test",
                             workspace_id=workspace_id, frequency="once", timezone="UTC", created_at=now, updated_at=now))
        db.flush()
        db.add(ScheduledRun(id=run_id, task_id=task_id, session_id=session_id, trigger_type="manual",
                            status="awaiting_approval", created_at=now))
    try:
        yield session_id, owner, run_id
    finally:
        runtime.invalidate_agents(session_id)
        approvals.forget_pending(session_id)
        with SessionLocal.begin() as db:
            db.delete(db.get(ScheduledRun, run_id))
            db.flush()
            db.delete(db.get(ScheduledTask, task_id))
            db.delete(db.get(Session, session_id))
            db.delete(db.get(Workspace, workspace_id))


@pytest.mark.parametrize("delegated", [False, True])
def test_scheduled_commands_require_approval_after_graph_restart(scheduled_session, tmp_path, monkeypatch, delegated):
    session_id, owner, _ = scheduled_session
    commands = []

    async def fake_execute(self, command, *, timeout=None):
        commands.append(command)
        return ExecuteResponse(output="ok", exit_code=0)

    async def forbidden_mcp(*args):
        pytest.fail("Scheduled graphs must not load MCP tools")

    monkeypatch.setattr(settings, "harness_allow_full_access", True)
    monkeypatch.setattr(settings, "harness_machine_access", True)
    monkeypatch.setattr(settings, "harness_machine_mounts", f"host={tmp_path}")
    monkeypatch.setattr(settings, "sandbox_mount_machine_dirs", True)
    monkeypatch.setattr(settings, "mcp_enabled", True)
    monkeypatch.setattr(settings, "intent_label_enabled", False)
    monkeypatch.setattr(access, "_global_mode", access.MODE_FULL)
    monkeypatch.setattr(permissions, "_global_hitl", False)
    monkeypatch.setattr(runtime, "build_mcp_tools", forbidden_mcp)
    monkeypatch.setattr(HybridDefaultBackend, "aexecute", fake_execute)
    monkeypatch.setattr(runtime, "_resolve_provider_for_agent", lambda *args, **kwargs: None)
    monkeypatch.setitem(runtime._agents, session_id, (object(), object()))

    shell = AIMessage(content="", tool_calls=[{"name": "execute", "args": {"command": "echo test"}, "id": "shell"}])
    delegate = AIMessage(content="", tool_calls=[{"name": "task", "args": {"subagent_type": "researcher",
                               "description": "run a command"}, "id": "delegate"}])
    monkeypatch.setattr(runtime, "build_llm", lambda _: FakeScheduleModel(responses=[delegate, shell] if delegated else [shell]))

    async def run():
        config = {"configurable": {"thread_id": session_id}}
        store = InMemoryStore()

        async def get_store():
            return store

        monkeypatch.setattr(runtime, "get_store", get_store)
        async with AsyncSqliteSaver.from_conn_string(str(tmp_path / "checkpoint.sqlite")) as saver:
            async def get_saver():
                return saver

            monkeypatch.setattr(runtime, "get_checkpointer", get_saver)
            backend = runtime.get_main_backend(session_id, owner)
            assert isinstance(backend.default, HybridDefaultBackend)
            assert set(backend.routes) == {sandbox.PERMISSION_ROUTE_PREFIX}
            graph = await runtime.get_agent(session_id, owner)
            await graph.ainvoke({"messages": [{"role": "user", "content": "start"}]}, config)
            pending = await approvals.read_pending_approval(graph, config)
            assert [action["tool"] for action in pending["actions"]] == ["execute"]
            assert commands == []

        runtime.invalidate_agents(session_id)
        replies = [AIMessage(content="child finished"), AIMessage(content="finished")] if delegated else [AIMessage(content="finished")]
        monkeypatch.setattr(runtime, "build_llm", lambda _: FakeScheduleModel(responses=replies))
        async with AsyncSqliteSaver.from_conn_string(str(tmp_path / "checkpoint.sqlite")) as saver:
            history_graph = await runtime.get_agent(session_id, owner, history_only=True)
            restored = await approvals.read_pending_approval(history_graph, config)
            assert restored["interrupts"] == pending["interrupts"]
            graph = await runtime.get_agent(session_id, owner)
            result = await graph.ainvoke(runtime.resume_input(approvals.build_resume_map(restored["interrupts"], True)), config)
            assert result["messages"][-1].content == "finished"
            assert commands == ["echo test"]

    asyncio.run(run())


def test_scheduled_access_settings_and_terminal_resume(scheduled_session, auth_client, monkeypatch):
    session_id, owner, run_id = scheduled_session
    monkeypatch.setattr(settings, "harness_allow_full_access", True)
    monkeypatch.setattr(access, "_global_mode", access.MODE_FULL)
    monkeypatch.setattr(permissions, "_global_hitl", False)
    approvals._note_pending(session_id, owner)
    assert auth_client.get(f"/api/v1/access/config?session_id={session_id}").json()["valid_modes"] == ["sandbox"]
    assert auth_client.get(f"/api/v1/hitl/config?session_id={session_id}").json()["enabled"] is True
    assert auth_client.put("/api/v1/access/config", json={"session_id": session_id, "mode": "full"}).status_code == 409
    assert auth_client.put("/api/v1/hitl/config", json={"session_id": session_id, "enabled": False}).status_code == 409
    assert asyncio.run(clear_pending_interrupt(None, user_id=owner)) == []
    assert session_id in approvals.known_pending_sessions(owner)

    with SessionLocal.begin() as db:
        db.get(ScheduledRun, run_id).status = "stopped"
    approval_url = f"/api/v1/approvals/{session_id}"
    assert auth_client.get(approval_url).json()["pending"] is False
    assert auth_client.post(approval_url, json={"decision": "approve"}).status_code == 409
    assert auth_client.post(f"{approval_url}/answer", json={"interrupt_id": "q", "answer": "a"}).status_code == 409


def test_scheduled_provider_does_not_fall_back(scheduled_session):
    _, owner, _ = scheduled_session
    chosen_id, default_id = uuid4().hex, uuid4().hex
    with SessionLocal.begin() as db:
        db.add_all([
            LlmProvider(id=chosen_id, user_id=owner, name="chosen", protocol="openai", model="chosen-model",
                        api_key_encrypted=encrypt_secret("chosen-key"), enabled=True),
            LlmProvider(id=default_id, user_id=owner, name="default", protocol="openai", model="default-model",
                        api_key_encrypted=encrypt_secret("default-key"), enabled=True, is_default=True),
        ])
    try:
        assert runtime._resolve_provider_for_agent(owner, chosen_id, scheduled=True).provider_id == chosen_id
        with SessionLocal.begin() as db:
            db.get(LlmProvider, chosen_id).enabled = False
        with pytest.raises(ProviderNotConfigured, match="已停用"):
            runtime._resolve_provider_for_agent(owner, chosen_id, scheduled=True)
        assert runtime._resolve_provider_for_agent(owner, None, scheduled=True).provider_id == default_id
        with SessionLocal.begin() as db:
            chosen = db.get(LlmProvider, chosen_id)
            chosen.enabled = True
            chosen.api_key_encrypted = "invalid ciphertext"
        with pytest.raises(ProviderNotConfigured, match="无法解密"):
            runtime._resolve_provider_for_agent(owner, chosen_id, scheduled=True)
    finally:
        with SessionLocal.begin() as db:
            db.delete(db.get(LlmProvider, chosen_id))
            db.delete(db.get(LlmProvider, default_id))


def test_scheduled_container_request_has_no_host_volumes(monkeypatch):
    from opensandbox.sync import SandboxSync
    from app.agent import opensandbox_backend

    volumes = []

    def capture_create(image, **kwargs):
        volumes.append(kwargs.get("volumes"))
        return object()

    def forbidden_volumes():
        pytest.fail("Scheduled container must not use configured host volumes")

    monkeypatch.setattr(SandboxSync, "create", capture_create)
    monkeypatch.setattr(opensandbox_backend, "_build_sandbox_volumes", forbidden_volumes)
    monkeypatch.setattr(opensandbox_backend, "_ensure_sandbox_root", lambda *args: None)
    assert SandboxRegistry._create("scheduled", allow_machine_mounts=False).volumes_key == ""
    assert volumes == [None]


def test_runner_observes_start_and_pause_or_completion():
    manager = RunManager()
    statuses = []
    manager.set_status_observer(lambda run: statuses.append((run.status, run.finished_at is not None)))

    async def run():
        async def paused():
            yield "approval", {"pending": True, "actions": [{"tool": "execute"}]}

        async def done():
            yield "result", "done"

        waiting = manager.start("test-session", {}, event_stream=paused)
        await manager.wait_result(waiting)
        resumed = manager.start("test-session", runtime.resume_input({"decisions": [{"type": "approve"}]}), event_stream=done)
        await manager.wait_result(resumed)
        await manager.aclose()

    asyncio.run(run())
    assert statuses == [("running", False), ("awaiting_approval", True), ("running", False), ("done", True)]
