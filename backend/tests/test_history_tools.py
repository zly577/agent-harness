import asyncio
import json

import pytest
from langchain_core.messages import HumanMessage

from app.agent import context
from app.agent.conversation_history import archive_messages
from app.agent.history_tools import build_history_tools
from app.db.models import Session as DbSession
from app.db.session import SessionLocal


@pytest.fixture(autouse=True)
def archive_database(monkeypatch, tmp_path):
    asyncio.run(context.aclose())
    monkeypatch.setattr(context.settings, "sqlite_path", str(tmp_path / "recall.sqlite"))
    with SessionLocal() as db:
        rows = [("recall-a", "owner", "project-a"), ("recall-b", "owner", "project-a"),
                ("recall-c", "owner", "project-b"), ("recall-d", "other", "project-a")]
        for sid, uid, wid in rows:
            db.add(DbSession(id=sid, user_id=uid, workspace_id=wid))
        db.commit()
    yield
    asyncio.run(context.aclose())
    with SessionLocal() as db:
        db.query(DbSession).filter(DbSession.id.like("recall-%")).delete(synchronize_session=False)
        db.commit()


def test_recall_is_scoped_and_paged_even_with_guessed_ids():
    search, read = build_history_tools("recall-a", "owner")
    async def run():
        for sid in ("recall-a", "recall-b", "recall-c", "recall-d"):
            await archive_messages(sid, [HumanMessage(content="约束：不能修改接口。" + "data" * 2000, id="same-id")])
        results = json.loads(await search.ainvoke({"query": "约束"}))
        assert [r["session_id"] for r in results] == ["recall-a"]
        results = json.loads(await search.ainvoke({"query": "不能修改接口", "scope": "workspace"}))
        assert {r["session_id"] for r in results} == {"recall-a", "recall-b"}
        assert json.loads(await read.ainvoke({"message_ids": ["recall-c/same-id", "recall-d/same-id"], "scope": "workspace"})) == []
        first = json.loads(await read.ainvoke({"message_ids": ["recall-b/same-id"], "scope": "workspace", "max_chars": 100}))[0]
        assert len(first["content"]) == 100 and first["next_offset"] == 100
        second = json.loads(await read.ainvoke({"message_ids": ["recall-b/same-id"], "scope": "workspace", "offset": 100, "max_chars": 100}))[0]
        assert len(second["content"]) == 100 and second["next_offset"] == 200
        assert json.loads(await read.ainvoke({"message_ids": ["recall-b/same-id"]})) == []
        # Authorization is refreshed rather than captured at graph construction.
        with SessionLocal() as db:
            db.delete(db.get(DbSession, "recall-a"))
            db.commit()
        assert json.loads(await search.ainvoke({"query": "约束", "scope": "workspace"})) == []
    asyncio.run(run())


def test_partial_index_updates_restart_and_literal_wildcards():
    from app.agent import conversation_history as history
    search, _ = build_history_tools("recall-a", "owner")
    async def run():
        await archive_messages("recall-a", [HumanMessage(content="old partial", id="m")], partial=True)
        await archive_messages("recall-a", [HumanMessage(content="约定 100%_done", id="m")])
        assert json.loads(await search.ainvoke({"query": "old partial"})) == []
        assert len(json.loads(await search.ainvoke({"query": "100%_done"}))) == 1
        await context.aclose()
        assert len(json.loads(await search.ainvoke({"query": "约定"}))) == 1
        # Exercise the no-FTS fallback with the same authorization and literal semantics.
        history._fts_available = False
        assert len(json.loads(await search.ainvoke({"query": "100%_done"}))) == 1
        assert json.loads(await search.ainvoke({"query": "absent%"})) == []
    asyncio.run(run())


def test_legacy_archive_backfill_does_not_index_inline_images():
    from langchain_core.messages import message_to_dict
    search, _ = build_history_tools("recall-a", "owner")
    async def run():
        conn = await context._ensure_conn()
        await conn.execute("""CREATE TABLE conversation_messages (
            seq INTEGER PRIMARY KEY AUTOINCREMENT, session_id TEXT NOT NULL, message_id TEXT NOT NULL,
            payload TEXT NOT NULL, partial INTEGER NOT NULL DEFAULT 0, UNIQUE(session_id,message_id))""")
        message = HumanMessage(content=[{"type": "text", "text": "旧版约束"},
            {"type": "image_url", "image_url": {"url": "data:image/png;base64,DO_NOT_INDEX_IMAGE_BYTES"}}], id="legacy")
        await conn.execute("INSERT INTO conversation_messages(session_id,message_id,payload) VALUES (?,?,?)",
                           ("recall-a", "legacy", json.dumps(message_to_dict(message), ensure_ascii=False)))
        await conn.commit()
        assert len(json.loads(await search.ainvoke({"query": "旧版约束"}))) == 1
        assert json.loads(await search.ainvoke({"query": "DO_NOT_INDEX_IMAGE_BYTES"})) == []
    asyncio.run(run())


def test_missing_fts_module_keeps_archive_writes_and_recall_working(monkeypatch):
    import aiosqlite
    search, _ = build_history_tools("recall-a", "owner")
    original = aiosqlite.Connection.execute
    def without_fts(self, sql, *args, **kwargs):
        if "CREATE VIRTUAL TABLE" in sql:
            raise aiosqlite.OperationalError("no such module: fts5")
        return original(self, sql, *args, **kwargs)
    monkeypatch.setattr(aiosqlite.Connection, "execute", without_fts)
    async def run():
        await archive_messages("recall-a", [HumanMessage(content="缺少索引仍能召回", id="fallback")])
        assert len(json.loads(await search.ainvoke({"query": "仍能召回"}))) == 1
    asyncio.run(run())
