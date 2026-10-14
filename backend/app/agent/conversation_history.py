"""Durable display history, independent of the graph's compressed context."""
import asyncio
import hashlib
import json

from langchain_core.messages import RemoveMessage, message_to_dict, messages_from_dict

from app.agent.context import _ensure_conn

_lock = asyncio.Lock()
_ready_conn = None


def is_internal_summary(message):
    return getattr(message, "additional_kwargs", {}).get("lc_source") == "summarization"


async def _connection():
    global _ready_conn
    conn = await _ensure_conn()
    if _ready_conn is conn:
        return conn
    await conn.execute("""CREATE TABLE IF NOT EXISTS conversation_messages (
        seq INTEGER PRIMARY KEY AUTOINCREMENT,
        session_id TEXT NOT NULL,
        message_id TEXT NOT NULL,
        payload TEXT NOT NULL,
        partial INTEGER NOT NULL DEFAULT 0,
        UNIQUE(session_id, message_id)
    )""")
    await conn.execute("""CREATE TABLE IF NOT EXISTS conversation_archive_cursor (
        session_id TEXT PRIMARY KEY, checkpoint_id TEXT NOT NULL
    )""")
    await conn.commit()
    _ready_conn = conn
    return conn


async def archive_messages(session_id, messages, *, partial=False):
    rows = []
    for message in messages:
        if isinstance(message, RemoveMessage) or is_internal_summary(message):
            continue
        payload = json.dumps(message_to_dict(message), ensure_ascii=False)
        # Graph messages normally have IDs; the hash supports old ID-less data.
        key = message.id or hashlib.sha256(payload.encode()).hexdigest()
        rows.append((session_id, key, payload, int(partial)))
    if not rows:
        return
    async with _lock:
        conn = await _connection()
        await conn.executemany("""INSERT INTO conversation_messages
            (session_id, message_id, payload, partial) VALUES (?, ?, ?, ?)
            ON CONFLICT(session_id, message_id) DO UPDATE SET
                payload=excluded.payload, partial=excluded.partial
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
