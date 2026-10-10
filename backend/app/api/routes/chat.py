"""对话路由（MySQL 持久化会话校验；归属隔离）。

一次性的非流式对话接口（`POST /api/v1/chat`）。前端走的是流式那条
（`/api/v1/chat/stream`），这个端点主要给脚本/探针用。
"""
from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session as OrmSession

from app.agent import runtime
from app.api.deps import get_current_user, owned_session_or_404
from app.agent.providers import ProviderNotConfigured
from app.api.routes.chat_stream import apply_provider_choice
from app.core.crypto import scrub_secrets
from app.db.models import User
from app.db.session import get_db
from app.schemas.chat import ChatRequest, ChatResponse

router = APIRouter(prefix="/api/v1", tags=["chat"])


@router.post("/chat", response_model=ChatResponse)
async def chat(
    req: ChatRequest,
    user: User = Depends(get_current_user),
    db: OrmSession = Depends(get_db),
):
    # 会话解析：无 session_id 则新建（登记 MySQL，绑定当前用户）；有则校验归属
    if req.session_id is None:
        session_id = runtime.create_session(user.id, runtime.session_title(req.message))
    else:
        owned_session_or_404(db, req.session_id, user)
        session_id = req.session_id
        runtime.touch_session(session_id)

    # 与流式入口共用同一段逻辑（含归属校验 + agent 重建），行为必须一致
    apply_provider_choice(db, session_id, user, req.provider_id)

    try:
        reply = await runtime.run_chat(session_id, req.message, user_id=user.id)
    except ProviderNotConfigured:
        # 交给 main.py 的全局处理器转 400（"还没配模型"是用户能自己解决的前置条件）。
        raise
    except Exception as e: # 其它模型/运行时错误统一转 500（兜底；中间件细化）
        # ⚠️ 必须过 scrub_secrets：供应商 SDK 的报错里可能带请求头或 URL
        # （见过 `...?api_key=sk-xxx` 这种），而这条 detail 会原样回给前端。
        raise HTTPException(
            status_code=500,
            detail=scrub_secrets(f"agent error: {type(e).__name__}: {e}"),
        )

    return ChatResponse(session_id=session_id, reply=reply)
