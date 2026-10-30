"""Personal settings persistence, ownership, memory conflicts, and real budgets."""
import asyncio
import pytest

from app.agent import runtime
from app.core.config import settings
from app.core.preferences import load_preferences
from app.db.models import UserPreferences, Workspace
from app.db.session import SessionLocal
from tests.helpers import make_auth_client


@pytest.fixture
def personal_client(monkeypatch):
    for key, value in {"context_max_messages": 80, "context_keep_messages": 24,
                       "tool_call_run_limit": 0, "tool_call_thread_limit": 0,
                       "tool_call_limit_tool": "web_search"}.items():
        monkeypatch.setattr(settings, key, value)
    client, user = make_auth_client()
    yield client, user
    client.close()


def test_preferences_persist_isolate_reset_and_invalidate(personal_client, monkeypatch):
    client, user = personal_client
    other, other_user = make_auth_client()
    dropped = []
    monkeypatch.setattr(runtime, "invalidate_agents", lambda **kwargs: dropped.append(kwargs))
    initial = client.get('/api/v1/preferences').json()
    assert initial['customized'] is False
    values = {**initial['values'], 'context_max_messages': 100, 'context_keep_messages': 30,
              'tool_call_run_limit': 3, 'tool_call_thread_limit': 12, 'tool_call_limit_tool': ''}
    response = client.put('/api/v1/preferences', json=values)
    assert response.status_code == 200, response.text
    assert response.json()['values'] == values
    assert response.json()['customized'] is True
    assert dropped == [{'user_id': user['id']}]
    # Runtime uses a separate database session, rather than request-local state.
    assert load_preferences(user['id']) == values
    assert load_preferences(other_user['id']) == initial['values']
    assert other.get('/api/v1/preferences').json() == initial
    with SessionLocal() as db:
        assert db.get(UserPreferences, user['id']) is not None
    assert client.delete('/api/v1/preferences').json() == initial
    assert load_preferences(user['id']) == initial['values']
    other.close()


@pytest.mark.parametrize('updates', [
    {'context_max_messages': 9}, {'context_max_messages': 1001},
    {'context_keep_messages': 80}, {'context_keep_messages': 0},
    {'tool_call_run_limit': -1}, {'tool_call_thread_limit': 1.5},
    {'tool_call_run_limit': 5, 'tool_call_thread_limit': 4},
    {'tool_call_run_limit': True}, {'tool_call_limit_tool': '../file'},
    {'jwt_secret': 'do-not-accept'},
    {'context_window_tokens': 4096, 'context_output_reserve': 4096},
    {'context_trigger_percent': 99}, {'context_keep_tokens': 0},
])
def test_preference_validation_rejects_without_saving(personal_client, updates):
    client, _ = personal_client
    initial = client.get('/api/v1/preferences').json()
    assert client.put('/api/v1/preferences', json={**initial['values'], **updates}).status_code == 422
    assert client.get('/api/v1/preferences').json() == initial


def test_memory_workspace_ownership_conflict_and_empty_save(personal_client, tmp_path):
    client, user = personal_client
    project = tmp_path / '中文项目'
    project.mkdir()
    workspace_id = 'e' * 32
    with SessionLocal() as db:
        db.add(Workspace(id=workspace_id, user_id=user['id'], name=project.name, directory=str(project)))
        db.commit()
    url = f'/api/v1/preferences/memory?workspace_id={workspace_id}'
    initial = client.get(url).json()
    assert initial == {'content': '', 'revision': 'missing', 'exists': False,
                       'core_limit': 6000, 'injected_chars': 0, 'detail_directory': '/workspace/.agent-memory/'}
    assert not (project / 'Agent.md').exists()
    saved = client.put(url, json={'content': '# 项目偏好\n请用中文。', 'revision': initial['revision']})
    assert saved.status_code == 200, saved.text
    assert (project / 'Agent.md').read_text(encoding='utf-8') == saved.json()['content']
    # An agent changed the file while the editor was open: preserve its newer edit.
    (project / 'Agent.md').write_text('来自智能体的更新', encoding='utf-8')
    conflict = client.put(url, json={'content': '过期编辑', 'revision': saved.json()['revision']})
    assert conflict.status_code == 409
    assert (project / 'Agent.md').read_text(encoding='utf-8') == '来自智能体的更新'
    other, _ = make_auth_client()
    assert other.get(url).status_code == 404
    assert other.put(url, json={'content': '越权', 'revision': 'missing'}).status_code == 404
    other.close()
    latest = client.get(url).json()
    empty = client.put(url, json={'content': '', 'revision': latest['revision']})
    assert empty.status_code == 200 and empty.json()['content'] == ''
    assert (project / 'Agent.md').read_bytes() == b''
    assert client.get('/api/v1/preferences/memory').json()['content'] == ''


def test_memory_rejects_oversize_non_utf8_and_symlink(personal_client, tmp_path):
    client, user = personal_client
    from app.core.workspace import ensure_workspace
    path = ensure_workspace(user['id']) / 'Agent.md'
    url = '/api/v1/preferences/memory'
    assert client.put(url, json={'content': 'a' * 100001, 'revision': 'missing'}).status_code == 422
    path.write_bytes(b'\xff')
    assert client.get(url).status_code == 422
    path.unlink()
    outside = tmp_path / 'outside.md'
    outside.write_text('private')
    try:
        path.symlink_to(outside)
    except OSError:
        pytest.skip('Symlinks unavailable')
    assert client.get(url).status_code == 403
    assert client.put(url, json={'content': 'changed', 'revision': 'missing'}).status_code == 403
    assert outside.read_text() == 'private'


def test_concurrent_memory_editors_cannot_overwrite_same_revision(personal_client):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Barrier
    client, _ = personal_client
    url = '/api/v1/preferences/memory'
    revision = client.get(url).json()['revision']
    barrier = Barrier(2)

    def save(content):
        barrier.wait(timeout=5)
        return client.put(url, json={'content': content, 'revision': revision})

    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [pool.submit(save, content) for content in ('first editor', 'second editor')]
        responses = [future.result(timeout=10) for future in futures]
    assert sorted(response.status_code for response in responses) == [200, 409]
    winner = next(response.json()['content'] for response in responses if response.status_code == 200)
    assert client.get(url).json()['content'] == winner


@pytest.mark.parametrize('method,path,body', [
    ('GET', '/api/v1/preferences', None), ('PUT', '/api/v1/preferences', {}),
    ('DELETE', '/api/v1/preferences', None), ('GET', '/api/v1/preferences/memory', None),
    ('PUT', '/api/v1/preferences/memory', {}),
])
def test_preferences_require_authentication(anon_client, method, path, body):
    assert anon_client.request(method, path, json=body).status_code == 401


def test_budget_blocks_real_tool_and_survives_graph_rebuild(monkeypatch):
    from langchain.agents import create_agent
    from langchain_core.messages import AIMessage
    from langchain_core.language_models.fake_chat_models import FakeMessagesListChatModel
    from langchain_core.tools import tool
    from langgraph.checkpoint.memory import InMemorySaver
    from app.agent.middleware import build_tool_call_limit_middlewares
    monkeypatch.setattr(settings, 'tool_call_limit_exit_behavior', 'continue')

    class Model(FakeMessagesListChatModel):
        def bind_tools(self, tools, **kwargs):
            return self

    executed = []

    @tool
    def work() -> str:
        """Perform a counted unit of work."""
        executed.append(True)
        return 'worked'

    saver = InMemorySaver()
    preferences = {'tool_call_thread_limit': 1, 'tool_call_run_limit': 0, 'tool_call_limit_tool': ''}
    from itertools import count
    builds = count()

    def graph():
        index = next(builds)
        return create_agent(Model(responses=[
            AIMessage(content='', id=f'ai-{index}', tool_calls=[{'name': 'work', 'args': {}, 'id': f'work-call-{index}'}]),
            AIMessage(content='done', id=f'done-{index}'),
        ]), tools=[work], middleware=build_tool_call_limit_middlewares(preferences=preferences), checkpointer=saver)

    async def check():
        config = {'configurable': {'thread_id': 'budget-thread'}}
        await graph().ainvoke({'messages': [{'role': 'user', 'content': 'first'}]}, config)
        assert len(executed) == 1
        # Rebuilding is how saving personal settings takes effect.
        result = await graph().ainvoke({'messages': [{'role': 'user', 'content': 'second'}]}, config)
        assert len(executed) == 1
        assert any(getattr(msg, 'status', None) == 'error' and getattr(msg, 'tool_call_id', None) == 'work-call-1' for msg in result['messages'])
        await graph().ainvoke({'messages': [{'role': 'user', 'content': 'separate'}]}, {'configurable': {'thread_id': 'other-thread'}})
        assert len(executed) == 2

    asyncio.run(check())


def test_summary_uses_personal_message_limits(tmp_path):
    from app.agent.context import build_summary_middleware
    from app.agent.history_model import HistoryModel
    from deepagents.backends import FilesystemBackend
    middleware = build_summary_middleware(HistoryModel(), backend=FilesystemBackend(root_dir=tmp_path, virtual_mode=True), preferences={'context_max_messages': 120, 'context_keep_messages': 40})
    assert middleware.trigger[0] == 'tokens'
    assert middleware.keep == ('tokens', 6000)


def test_legacy_clients_preserve_new_context_settings(personal_client):
    client, _ = personal_client
    values = client.get('/api/v1/preferences').json()['values']
    values.update(context_window_tokens=16000, context_output_reserve=2000, context_keep_tokens=3000)
    assert client.put('/api/v1/preferences', json=values).status_code == 200
    legacy = {key: value for key, value in values.items() if key in (
        'context_max_messages', 'context_keep_messages', 'tool_call_run_limit', 'tool_call_thread_limit', 'tool_call_limit_tool')}
    legacy['tool_call_run_limit'] = 2
    result = client.put('/api/v1/preferences', json=legacy)
    assert result.status_code == 200
    assert result.json()['values'] == {**values, 'tool_call_run_limit': 2}


@pytest.mark.parametrize('tool_name', ['', 'work'])
def test_run_budget_resets_and_respects_tool_scope(monkeypatch, tool_name):
    from langchain.agents import create_agent
    from langchain_core.messages import AIMessage
    from langchain_core.language_models.fake_chat_models import FakeMessagesListChatModel
    from langchain_core.tools import tool
    from langgraph.checkpoint.memory import InMemorySaver
    from app.agent.middleware import build_tool_call_limit_middlewares
    monkeypatch.setattr(settings, 'tool_call_limit_exit_behavior', 'continue')
    class Model(FakeMessagesListChatModel):
        def bind_tools(self, tools, **kwargs):
            return self
    executed = []
    @tool
    def work() -> str:
        """Counted work."""
        executed.append('work')
        return 'ok'
    @tool
    def other() -> str:
        """Other work."""
        executed.append('other')
        return 'ok'
    responses = []
    for index in range(2):
        responses.extend([AIMessage(content='', id=f'ai-{index}', tool_calls=[
            {'name': 'work', 'args': {}, 'id': f'first-{index}'},
            {'name': 'work', 'args': {}, 'id': f'second-{index}'},
            {'name': 'other', 'args': {}, 'id': f'third-{index}'},
        ]), AIMessage(content='done', id=f'done-{index}')])
    model = Model(responses=responses)
    graph = create_agent(model, tools=[work, other], middleware=build_tool_call_limit_middlewares(preferences={
        'tool_call_run_limit': 1, 'tool_call_thread_limit': 0, 'tool_call_limit_tool': tool_name,
    }), checkpointer=InMemorySaver())
    async def check():
        config = {'configurable': {'thread_id': 'run-budget'}}
        for index in range(2):
            await graph.ainvoke({'messages': [{'role': 'user', 'content': str(index)}]}, config)
        assert executed.count('work') == 2
        assert executed.count('other') == (2 if tool_name else 0)
    asyncio.run(check())


def test_personal_preferences_migration_is_idempotent(tmp_path, monkeypatch):
    import importlib.util
    from pathlib import Path
    from alembic.migration import MigrationContext
    from alembic.operations import Operations
    from sqlalchemy import create_engine, inspect, text
    path = Path(__file__).parents[1] / 'alembic/versions/0008_user_preferences.py'
    spec = importlib.util.spec_from_file_location('preferences_migration', path)
    migration = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(migration)
    engine = create_engine('sqlite:///' + str(tmp_path / 'migration.sqlite'))
    try:
        with engine.begin() as connection:
            monkeypatch.setattr(migration, 'op', Operations(MigrationContext.configure(connection)))
            migration.upgrade()
            connection.execute(text("INSERT INTO user_preferences (user_id, values_json) VALUES ('user', '{}')"))
            migration.upgrade()
            assert connection.execute(text('SELECT COUNT(*) FROM user_preferences')).scalar() == 1
            assert {column['name'] for column in inspect(connection).get_columns('user_preferences')} == {'user_id', 'values_json'}
            migration.downgrade()
            assert 'user_preferences' not in inspect(connection).get_table_names()
    finally:
        engine.dispose()
