"""JSON chat transport; preparation and execution are shared with SSE."""
from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session as OrmSession

from app.agent.providers import ProviderNotConfigured
from app.agent.runner import RunStopped, run_manager
from app.api.deps import get_current_user
from app.core.crypto import scrub_secrets
from app.db.models import User
from app.db.session import get_db
from app.schemas.chat import ChatRequest, ChatResponse
from app.services.chat import prepare_chat, start_chat

router = APIRouter(prefix="/api/v1", tags=["chat"])


@router.post("/chat", response_model=ChatResponse)
async def chat(
    req: ChatRequest,
    user: User = Depends(get_current_user),
    db: OrmSession = Depends(get_db),
):
    prepared = await prepare_chat(req, user, db)
    run = start_chat(prepared)
    try:
        reply = await run_manager.wait_result(run)
    except ProviderNotConfigured:
        raise
    except RunStopped as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except Exception as exc:
        raise HTTPException(
            status_code=500,
            detail=scrub_secrets(f"agent error: {type(exc).__name__}: {exc}"),
        ) from exc
    return ChatResponse(session_id=prepared.session_id, reply=reply)
