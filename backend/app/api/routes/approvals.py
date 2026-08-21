"""审批路由（P8）：列出待审批 / 批准 / 拒绝。

- GET  /api/v1/approvals/{session_id}     查看某会话当前待审批项
- POST /api/v1/approvals/{session_id}     body: {"decision": "approve"|"reject"}，审批并恢复
"""
from fastapi import APIRouter, HTTPException

from app.agent import approvals, runtime
from app.schemas.approval import ApprovalCheckResponse, ApprovalDecideRequest, ApprovalDecideResponse

router = APIRouter(prefix="/api/v1/approvals", tags=["approvals"])


def _config(session_id: str) -> dict:
    return {"configurable": {"thread_id": session_id}}


@router.get("/{session_id}", response_model=ApprovalCheckResponse)
async def check_approval(session_id: str):
    """查看该会话当前是否有待审批项。"""
    agent = await runtime.get_agent()
    info = await approvals.read_pending_approval(agent, _config(session_id))
    return ApprovalCheckResponse(
        session_id=session_id,
        pending=info.get("pending", False),
        actions=info.get("actions") or [],
    )


@router.post("/{session_id}", response_model=ApprovalDecideResponse)
async def decide(session_id: str, req: ApprovalDecideRequest):
    """批准或拒绝当前待审批项，并恢复会话执行。"""
    agent = await runtime.get_agent()
    info = await approvals.read_pending_approval(agent, _config(session_id))
    if not info.get("pending"):
        raise HTTPException(status_code=409, detail="当前无待审批项")

    approve = req.decision == "approve"
    decisions = approvals.build_decision(info.get("actions"), approve=approve)
    try:
        result = await approvals.decide(agent, _config(session_id), decisions)
        reply = str(result["messages"][-1].content)[:2000]
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"审批恢复失败: {type(e).__name__}: {e}")

    return ApprovalDecideResponse(
        session_id=session_id,
        decided="approve" if approve else "reject",
        reply=reply,
    )
