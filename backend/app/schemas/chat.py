"""对话接口的请求/响应模型。"""
from pydantic import BaseModel, Field


class ChatRequest(BaseModel):
    message: str = Field(..., min_length=1, max_length=8000, description="用户消息")
    session_id: str | None = Field(None, description="会话 ID；为空则新建会话")
    workspace_id: str | None = Field(None, max_length=32, description="新会话所属工作区")
    #: 本轮使用的模型供应商。传了就写回会话（后续轮次沿用），
    #: 不传则按「会话已绑定 → 用户默认 → 系统默认」解析。
    #: 注意它**不是**"只影响这一轮"的临时值 —— 需求 §4.2.4 的语义是"会话记住模型选择"。
    provider_id: str | None = Field(
        None, description="模型供应商 id；不传则用会话/用户/系统默认"
    )


class ChatResponse(BaseModel):
    session_id: str
    reply: str
