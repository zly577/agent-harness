"""Shared chat preparation and execution for JSON and SSE endpoints."""
from dataclasses import dataclass
from typing import Any

from fastapi import HTTPException
from sqlalchemy.orm import Session

from app.agent import runtime
from app.agent.approvals import read_pending_approval
from app.agent.attachments import prepare_content, save_refusal, supports_vision
from app.agent import providers
from app.agent.runner import RunConflict, SessionRun, run_manager
from app.api.deps import owned_session_or_404
from app.db.models import User, Workspace
from app.schemas.chat import ChatRequest


@dataclass(frozen=True)
class PreparedChat:
    session_id: str
    user_id: str
    message: str
    content: Any
    refusal_message: str | None = None


def apply_provider_choice(db: Session, session_id: str, user: User, provider_id: str | None) -> None:
    if provider_id is None:
        return
    row = providers.get_owned_provider(db, provider_id, user.id)
    if row is None or not row.enabled:
        raise HTTPException(status_code=404, detail="provider not found or disabled")
    runtime.set_session_provider(session_id, provider_id)


async def prepare_chat(req: ChatRequest, user: User, db: Session) -> PreparedChat:
    """Validate ownership, pending input and model choice before starting a run."""
    content, has_images = prepare_content(req)
    if req.session_id is None:
        workspace_id = req.workspace_id if req.workspace_id != "legacy" else None
        if workspace_id:
            workspace = db.get(Workspace, workspace_id)
            if not workspace or workspace.user_id != user.id:
                raise HTTPException(status_code=404, detail="工作区不存在")
        session_id = runtime.create_session(user.id, runtime.session_title(req.message), workspace_id=workspace_id)
    else:
        row = owned_session_or_404(db, req.session_id, user)
        session_id = req.session_id
        if run_manager.is_running(session_id):
            raise HTTPException(status_code=409, detail="该会话已有一轮回答正在进行中")
        agent = await runtime.get_agent(session_id, user_id=user.id, history_only=True)
        pending = await read_pending_approval(agent, {"configurable": {"thread_id": session_id}}, user_id=user.id)
        if pending.get("pending"):
            raise HTTPException(status_code=409, detail="请先回答待澄清的问题或处理待审批操作，再发送新消息")
        # get_agent/read_pending_approval can yield to another incoming request.
        if run_manager.is_running(session_id):
            raise HTTPException(status_code=409, detail="该会话已有一轮回答正在进行中")
        if row.title == "新会话":
            row.title = runtime.session_title(req.message)
            db.commit()
        runtime.touch_session(session_id)

    apply_provider_choice(db, session_id, user, req.provider_id)
    refusal_message = None
    if has_images:
        cfg, _ = providers.resolve_provider(db, user.id, runtime.session_context(session_id)[1])
        if not supports_vision(cfg):
            refusal_message = req.message + "\n" + "\n".join("[附件：" + a.name + "]" for a in req.attachments)
    return PreparedChat(session_id, user.id, req.message, content, refusal_message)


def start_chat(chat: PreparedChat) -> SessionRun:
    """Reserve the session before any checkpoint write, including image refusals."""
    async def refusal_events():
        reply = await save_refusal(chat.session_id, chat.refusal_message, chat.user_id)
        yield "result", reply

    try:
        return run_manager.start(
            chat.session_id,
            {"messages": [{"role": "user", "content": chat.content}]},
            user_message=chat.message,
            user_id=chat.user_id,
            event_stream=refusal_events if chat.refusal_message is not None else None,
        )
    except RunConflict as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
