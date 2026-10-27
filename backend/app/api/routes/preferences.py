"""Authenticated personal settings and workspace-scoped memory editing."""
import hashlib
import json
import os
import tempfile
from threading import Lock

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.agent import runtime
from app.api.deps import get_current_user
from app.api.routes.files import _workspace_root
from app.core.preferences import preference_defaults, preferences_for
from app.db.models import User, UserPreferences
from app.db.session import get_db
from app.schemas.preferences import AgentPreferences, MemoryUpdate

router = APIRouter(prefix="/api/v1/preferences", tags=["preferences"])
# Compare-and-replace must be serialized across FastAPI's request threads.
_memory_write_lock = Lock()


@router.get("")
def get_preferences(user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    return {"values": preferences_for(db, user.id), "defaults": preference_defaults(),
            "customized": db.get(UserPreferences, user.id) is not None}


@router.put("")
def save_preferences(req: AgentPreferences, user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    row = db.get(UserPreferences, user.id)
    if row is None:
        row = UserPreferences(user_id=user.id)
        db.add(row)
    row.values_json = json.dumps(req.model_dump())
    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        # Another browser tab may have created this user's row concurrently.
        row = db.get(UserPreferences, user.id)
        if row is None:
            raise
        row.values_json = json.dumps(req.model_dump())
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


def _memory_path(user, workspace_id, db):
    root = _workspace_root(user, workspace_id, db).resolve()
    if not root.is_dir():
        raise HTTPException(status_code=409, detail="工作区目录已不存在")
    path = root / "Agent.md"
    if path.is_symlink() or path.resolve().parent != root:
        raise HTTPException(status_code=403, detail="长期记忆不能指向工作区外部或符号链接")
    return path


def _read_memory(path):
    if not path.exists():
        return {"content": "", "revision": "missing", "exists": False}
    if not path.is_file() or path.stat().st_size > 400000:
        raise HTTPException(status_code=422, detail="长期记忆必须为不超过 10 万字的 UTF-8 文本文件")
    try:
        data = path.read_bytes()
        content = data.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise HTTPException(status_code=422, detail="长期记忆必须为 UTF-8 文本") from exc
    if len(content) > 100000:
        raise HTTPException(status_code=422, detail="长期记忆不能超过 10 万字")
    return {"content": content, "revision": hashlib.sha256(data).hexdigest(), "exists": True}


@router.get("/memory")
def get_memory(workspace_id: str | None = None, user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    return _read_memory(_memory_path(user, workspace_id, db))


@router.put("/memory")
def save_memory(req: MemoryUpdate, workspace_id: str | None = None,
                user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    with _memory_write_lock:
        return _save_memory(_memory_path(user, workspace_id, db), req)


def _save_memory(path, req):
    if _read_memory(path)["revision"] != req.revision:
        raise HTTPException(status_code=409, detail="长期记忆已被其他页面或智能体修改，请重新加载后再编辑")
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", newline="", dir=path.parent,
                                         prefix=".agent-memory-", suffix=".tmp", delete=False) as stream:
            temporary = stream.name
            stream.write(req.content)
        if _read_memory(path)["revision"] != req.revision:
            raise HTTPException(status_code=409, detail="长期记忆已被修改，请重新加载后再编辑")
        os.replace(temporary, path)
    finally:
        if temporary and os.path.exists(temporary):
            os.unlink(temporary)
    return _read_memory(path)
