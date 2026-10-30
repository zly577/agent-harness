"""Authenticated personal settings and user-scoped memory editing."""
import json

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session
from pydantic import ValidationError

from app.agent import runtime
from app.api.deps import get_current_user
from app.core.workspace import ensure_workspace
from app.core.preferences import preference_defaults, preferences_for
from app.db.models import User, UserPreferences
from app.db.session import get_db
from app.schemas.preferences import AgentPreferences, MemoryUpdate
from app.agent.memory_storage import MEMORY_WRITE_LOCK, MemoryConflict, read_snapshot, write_snapshot
from app.core.config import settings

router = APIRouter(prefix="/api/v1/preferences", tags=["preferences"])
# Compare-and-replace must be serialized across FastAPI's request threads.
_memory_write_lock = MEMORY_WRITE_LOCK


@router.get("")
def get_preferences(user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    return {"values": preferences_for(db, user.id), "defaults": preference_defaults(),
            "context_fallback_tokens": settings.context_fallback_tokens,
            "customized": db.get(UserPreferences, user.id) is not None}


@router.put("")
def save_preferences(req: AgentPreferences, user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    values = preferences_for(db, user.id)
    values.update({key: value for key, value in req.model_dump().items() if key in req.model_fields_set})
    try:
        values = AgentPreferences(**values).model_dump()
    except ValidationError as exc:
        raise HTTPException(status_code=422, detail=exc.errors(include_context=False)) from exc
    row = db.get(UserPreferences, user.id)
    if row is None:
        row = UserPreferences(user_id=user.id)
        db.add(row)
    row.values_json = json.dumps(values)
    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        # Another browser tab may have created this user's row concurrently.
        row = db.get(UserPreferences, user.id)
        if row is None:
            raise
        row.values_json = json.dumps(values)
        db.commit()
    runtime.invalidate_agents(user_id=user.id)
    return get_preferences(user, db)


@router.delete("")
def reset_preferences(user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    row = db.get(UserPreferences, user.id)
    if row:
        db.delete(row)
        db.commit()
    runtime.invalidate_agents(user_id=user.id)
    return get_preferences(user, db)


def _memory_path(user):
    root = ensure_workspace(user.id).resolve()
    path = root / "Agent.md"
    if path.is_symlink() or path.resolve().parent != root:
        raise HTTPException(status_code=403, detail="长期记忆不能指向工作区外部或符号链接")
    return path


def _read_memory(path):
    try:
        return read_snapshot(path)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


def _memory_view(value):
    return {**value, "core_limit": settings.memory_core_max_chars,
            "injected_chars": min(len(value["content"]), settings.memory_core_max_chars),
            "detail_directory": "/memories/.agent-memory/"}


@router.get("/memory")
def get_memory(user: User = Depends(get_current_user)):
    return _memory_view(_read_memory(_memory_path(user)))


@router.put("/memory")
def save_memory(req: MemoryUpdate, user: User = Depends(get_current_user)):
    with _memory_write_lock:
        return _memory_view(_save_memory(_memory_path(user), req))


def _save_memory(path, req):
    try:
        return write_snapshot(path, req.content, req.revision)
    except MemoryConflict as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
