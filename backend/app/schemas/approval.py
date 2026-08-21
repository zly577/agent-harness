"""审批相关请求/响应模型（P8）。"""
from pydantic import BaseModel, Field


class ApprovalAction(BaseModel):
    type: str = "tool_call"
    tool: str = ""
    args: dict = Field(default_factory=dict)


class ApprovalCheckResponse(BaseModel):
    session_id: str
    pending: bool
    actions: list[ApprovalAction] = Field(default_factory=list)


class ApprovalDecideRequest(BaseModel):
    decision: str = Field(..., pattern="^(approve|reject)$", description="approve 或 reject")


class ApprovalDecideResponse(BaseModel):
    session_id: str
    decided: str
    reply: str
