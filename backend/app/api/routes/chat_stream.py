"""SSE 流式对话路由（P9）。

POST /api/v1/chat/stream  body: {"message","session_id"}
返回 text/event-stream，每行 data: <json>。

事件：
  - {"type":"token","content":"..."}      模型输出增量
  - {"type":"tool","name":"..."}          工具调用
  - {"type":"approval","pending":true,...} 需要人工审批
  - {"type":"result","content":"..."}      最终回复
  - {"type":"error","detail":"..."}       错误
"""
import asyncio
import json

from fastapi import APIRouter, HTTPException
from fastapi.responses import StreamingResponse

from app.agent import runtime
from app.schemas.chat import ChatRequest

router = APIRouter(prefix="/api/v1", tags=["chat-stream"])


async def _event_stream(session_id: str, message: str):
    """把 runtime.stream_chat 的事件转成 SSE data 行。"""
    def sse(obj: dict) -> str:
        return f"data: {json.dumps(obj, ensure_ascii=False)}\n\n"

    # 先推送 session 事件（前端据此记录 session_id）
    yield sse({"type": "session", "session_id": session_id})
    try:
        async for kind, data in runtime.stream_chat(session_id, message):
            if kind == "token":
                yield sse({"type": "token", "content": data})
            elif kind == "tool":
                yield sse({"type": "tool", "name": data})
            elif kind == "approval":
                yield sse({
                    "type": "approval",
                    "pending": bool(data.get("pending")),
                    "actions": data.get("actions") or [],
                })
            elif kind == "result":
                yield sse({"type": "result", "content": data})
        yield "event: done\ndata: {}\n\n"
    except Exception as e:
        yield sse({"type": "error", "detail": f"{type(e).__name__}: {e}"})


@router.post("/chat/stream")
async def chat_stream(req: ChatRequest):
    """SSE 流式对话。"""
    # 会话解析：无 session_id 则新建；有则校验
    if req.session_id is None:
        session_id = runtime.create_session()
    elif runtime.session_exists(req.session_id):
        session_id = req.session_id
    else:
        raise HTTPException(status_code=404, detail="session not found")

    return StreamingResponse(
        _event_stream(session_id, req.message),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )
