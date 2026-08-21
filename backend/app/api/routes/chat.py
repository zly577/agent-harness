"""对话路由（P1）。"""
from fastapi import APIRouter, HTTPException

from app.agent import runtime
from app.schemas.chat import ChatRequest, ChatResponse

router = APIRouter(prefix="/api/v1", tags=["chat"])


@router.post("/chat", response_model=ChatResponse)
async def chat(req: ChatRequest):
    # 会话解析：无 session_id 则新建；有则校验存在
    if req.session_id is None:
        session_id = runtime.new_session()
    elif runtime.has_session(req.session_id):
        session_id = req.session_id
    else:
        raise HTTPException(status_code=404, detail="session not found")

    try:
        reply = await runtime.run_chat(session_id, req.message)
    except Exception as e:  # 模型/运行时错误统一转 500（P1 兜底，P5 中间件细化）
        raise HTTPException(status_code=500, detail=f"agent error: {type(e).__name__}: {e}")

    return ChatResponse(session_id=session_id, reply=reply)
