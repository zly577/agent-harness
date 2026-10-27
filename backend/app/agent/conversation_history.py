"""Durable display history, independent of the graph's compressed context."""
import asyncio
import hashlib
import json
import aiosqlite

from langchain_core.messages import RemoveMessage, message_to_dict, messages_from_dict

from app.agent.context import _ensure_conn

_lock = asyncio.Lock()
_ready_conn = None
_fts_available = False


def is_internal_summary(message):
    return getattr(message, "additional_kwargs", {}).get("lc_source") == "summarization"


async def _connection():
    global _ready_conn, _fts_available
    conn = await _ensure_conn()
    if _ready_conn is conn:
        return conn
    await conn.execute("""CREATE TABLE IF NOT EXISTS conversation_messages (
        seq INTEGER PRIMARY KEY AUTOINCREMENT,
        session_id TEXT NOT NULL,
        message_id TEXT NOT NULL,
        payload TEXT NOT NULL,
        partial INTEGER NOT NULL DEFAULT 0,
        search_text TEXT,
        UNIQUE(session_id, message_id)
    )""")
    await conn.execute("""CREATE TABLE IF NOT EXISTS conversation_archive_cursor (
        session_id TEXT PRIMARY KEY, checkpoint_id TEXT NOT NULL
    )""")
    async with conn.execute("PRAGMA table_info(conversation_messages)") as cursor:
        columns = {row[1] for row in await cursor.fetchall()}
    migrated = "search_text" not in columns
    if migrated:
        await conn.execute("ALTER TABLE conversation_messages ADD COLUMN search_text TEXT")
    async with conn.execute("SELECT seq,payload FROM conversation_messages WHERE search_text IS NULL") as cursor:
        while batch := await cursor.fetchmany(100):
            await conn.executemany("UPDATE conversation_messages SET search_text=? WHERE seq=?",
                                   [(history_text(payload)[1], seq) for seq, payload in batch])
    try:
        # Trigram LIKE supports Chinese substrings; shorter queries still work.
        await conn.execute("""CREATE VIRTUAL TABLE IF NOT EXISTS conversation_search
            USING fts5(session_id UNINDEXED, message_id UNINDEXED, search_text, tokenize='trigram')""")
        await conn.execute("DROP TRIGGER IF EXISTS conversation_search_insert")
        await conn.execute("""CREATE TRIGGER conversation_search_insert AFTER INSERT ON conversation_messages
            BEGIN INSERT INTO conversation_search(rowid,session_id,message_id,search_text)
            VALUES (new.seq,new.session_id,new.message_id,new.search_text); END""")
        await conn.execute("DROP TRIGGER IF EXISTS conversation_search_update")
        await conn.execute("""CREATE TRIGGER conversation_search_update AFTER UPDATE OF search_text ON conversation_messages
            BEGIN DELETE FROM conversation_search WHERE rowid=old.seq;
            INSERT INTO conversation_search(rowid,session_id,message_id,search_text)
            VALUES (new.seq,new.session_id,new.message_id,new.search_text); END""")
        await conn.execute("""CREATE TRIGGER IF NOT EXISTS conversation_search_delete AFTER DELETE ON conversation_messages
            BEGIN DELETE FROM conversation_search WHERE rowid=old.seq; END""")
        if migrated:
            await conn.execute("DELETE FROM conversation_search")
        await conn.execute("""INSERT INTO conversation_search(rowid,session_id,message_id,search_text)
            SELECT seq,session_id,message_id,search_text FROM conversation_messages m
            WHERE NOT EXISTS (SELECT 1 FROM conversation_search s WHERE s.rowid=m.seq)""")
        _fts_available = True
    except aiosqlite.OperationalError as exc:
        if not any(reason in str(exc).lower() for reason in ("no such module", "no such tokenizer")):
            raise
        # FTS5/trigram is optional. Scoped keyword recall still works without it.
        _fts_available = False
        for name in ("insert", "update", "delete"):
            await conn.execute(f"DROP TRIGGER IF EXISTS conversation_search_{name}")
    await conn.commit()
    _ready_conn = conn
    return conn


def history_text(payload):
    message = messages_from_dict([json.loads(payload)])[0]
    content = message.content
    text = content if isinstance(content, str) else "\n".join(
        block.get("text", "") for block in content if isinstance(block, dict) and block.get("type") == "text")
    if getattr(message, "tool_calls", None):
        text += "\n" + json.dumps(message.tool_calls, ensure_ascii=False)
    return message.type, text


async def search_archive(session_ids, query, limit=5):
    if not session_ids or not query.strip():
        return []
    query = query.strip()[:200]
    escaped = any(char in query for char in "\\%_")
    pattern = "%" + (query.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") if escaped else query) + "%"
    escape_clause = " ESCAPE '\\'" if escaped else ""
    marks = ",".join("?" for _ in session_ids)
    async with _lock:
        conn = await _connection()
        if _fts_available:
            sql = f"""SELECT m.session_id,m.message_id,m.payload FROM conversation_search s
                JOIN conversation_messages m ON m.seq=s.rowid WHERE m.session_id IN ({marks})
                AND s.search_text LIKE ? {escape_clause} ORDER BY m.seq DESC LIMIT ?"""
        else:
            sql = f"""SELECT session_id,message_id,payload FROM conversation_messages
                WHERE session_id IN ({marks}) AND search_text LIKE ? {escape_clause} ORDER BY seq DESC LIMIT ?"""
        async with conn.execute(sql, [*session_ids, pattern, min(max(limit, 1), 20)]) as cursor:
            rows = await cursor.fetchall()
    results = []
    for sid, mid, payload in rows:
        role, text = history_text(payload)
        position = max(0, text.casefold().find(query.casefold()) - 100)
        results.append({"message_id": f"{sid}/{mid}", "session_id": sid, "role": role,
                        "excerpt": text[position:position + 600], "offset": position, "length": len(text)})
    return results


async def read_archive(session_ids, message_ids, current_session, offset=0, max_chars=6000):
    allowed = set(session_ids)
    pairs = []
    for handle in message_ids[:20]:
        sid, mid = handle.split("/", 1) if "/" in handle else (current_session, handle)
        if sid in allowed and (sid, mid) not in pairs:
            pairs.append((sid, mid))
    results, remaining = [], min(max(max_chars, 1), 12000)
    async with _lock:
        conn = await _connection()
        for sid, mid in pairs:
            async with conn.execute("SELECT payload FROM conversation_messages WHERE session_id=? AND message_id=?", (sid, mid)) as cursor:
                row = await cursor.fetchone()
            if not row or remaining <= 0:
                continue
            role, text = history_text(row[0])
            content = text[offset:offset + remaining]
            end = offset + len(content)
            results.append({"message_id": f"{sid}/{mid}", "role": role, "content": content,
                            "next_offset": end if end < len(text) else None, "length": len(text)})
            remaining -= len(content)
    return results


async def archive_messages(session_id, messages, *, partial=False):
    rows = []
    for message in messages:
        if isinstance(message, RemoveMessage) or is_internal_summary(message):
            continue
        payload = json.dumps(message_to_dict(message), ensure_ascii=False)
        # Graph messages normally have IDs; the hash supports old ID-less data.
        key = message.id or hashlib.sha256(payload.encode()).hexdigest()
        rows.append((session_id, key, payload, int(partial), history_text(payload)[1]))
    if not rows:
        return
    async with _lock:
        conn = await _connection()
        await conn.executemany("""INSERT INTO conversation_messages
            (session_id, message_id, payload, partial, search_text) VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(session_id, message_id) DO UPDATE SET
                payload=excluded.payload, partial=excluded.partial, search_text=excluded.search_text
            WHERE conversation_messages.partial = 1""", rows)
        await conn.commit()


async def read_messages(session_id):
    async with _lock:
        conn = await _connection()
        async with conn.execute(
            "SELECT payload FROM conversation_messages WHERE session_id=? ORDER BY seq",
            (session_id,),
        ) as cursor:
            rows = await cursor.fetchall()
    return messages_from_dict([json.loads(row[0]) for row in rows])


async def sync_checkpoints(agent, session_id):
    """Backfill older checkpoints once, then import only newer snapshots.

    Use graph snapshots, not raw channel_values: message channels may contain
    deltas. Never write archive messages back into the model's state.
    """
    config = {"configurable": {"thread_id": session_id}}
    async with _lock:
        conn = await _connection()
        async with conn.execute(
            "SELECT checkpoint_id FROM conversation_archive_cursor WHERE session_id=?",
            (session_id,),
        ) as cursor:
            row = await cursor.fetchone()
    previous = row[0] if row else None
    snapshots = []
    if hasattr(agent, "aget_state_history"):
        async for snapshot in agent.aget_state_history(config):
            checkpoint_id = snapshot.config["configurable"].get("checkpoint_id", "")
            if previous and checkpoint_id == previous:
                break
            snapshots.append(snapshot)
        # Checkpoints are returned newest first; insertion order must be oldest first.
        for snapshot in reversed(snapshots):
            await archive_messages(session_id, (snapshot.values or {}).get("messages") or [])
        if snapshots:
            checkpoint_id = snapshots[0].config["configurable"].get("checkpoint_id", "")
            async with _lock:
                conn = await _connection()
                await conn.execute("""INSERT INTO conversation_archive_cursor VALUES (?, ?)
                    ON CONFLICT(session_id) DO UPDATE SET checkpoint_id=excluded.checkpoint_id""",
                    (session_id, checkpoint_id))
                await conn.commit()
    # Include pending writes in the current state (e.g. an interrupted tool).
    state = await agent.aget_state(config)
    await archive_messages(session_id, (state.values or {}).get("messages") or [])


async def archive_stream(source, session_id):
    """Retain partial main-model output when a streamed run is stopped or fails."""
    import time
    from langchain_core.messages import AIMessage, AIMessageChunk, message_chunk_to_message
    from app.agent.subagent_stream import normalize_chunk, parse_subagent_namespace

    accumulated = {}
    last_write = 0.0
    try:
        async for chunk in source:
            namespace, message, metadata = normalize_chunk(chunk)
            if (metadata.get("langgraph_node") == "model"
                    and not parse_subagent_namespace(namespace)
                    and not is_internal_summary(message)
                    and metadata.get("lc_source") != "summarization"
                    and "langchain:internal" not in metadata.get("tags", [])
                    and isinstance(message, AIMessage) and message.id):
                if isinstance(message, AIMessageChunk):
                    previous = accumulated.get(message.id)
                    accumulated[message.id] = previous + message if previous is not None else message
                else:
                    accumulated[message.id] = message
                if time.monotonic() - last_write >= 1:
                    await archive_messages(session_id, [message_chunk_to_message(m) for m in accumulated.values()], partial=True)
                    last_write = time.monotonic()
            yield chunk
    finally:
        if accumulated:
            await archive_messages(session_id, [message_chunk_to_message(m) for m in accumulated.values()], partial=True)
