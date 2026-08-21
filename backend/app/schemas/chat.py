"""对话接口的请求/响应模型。"""
from pydantic import BaseModel, Field


class ChatRequest(BaseModel):
    message: str = Field(..., min_length=1, max_length=8000, description="用户消息")
    session_id: str | None = Field(None, description="会话 ID；为空则新建会话")


class ChatResponse(BaseModel):
    session_id: str
    reply: str
