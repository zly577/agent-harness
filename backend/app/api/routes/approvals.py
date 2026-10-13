"""审批路由：待审批查询 / 批准 / 拒绝。

- GET  /api/v1/approvals/{session_id}   查看某会话当前待审批项
- POST /api/v1/approvals/{session_id}   body: {"decision":"approve"|"reject"}

人工介入的**开关**在 `app/api/routes/hitl.py`（`/api/v1/hitl/config`）。

POST 返回 **text/event-stream**（与 /chat/stream 同一套事件），恢复执行的
工具调用 / 模型输出会实时推给前端。这样做的原因：

1. 审批恢复往往要跑工具 + 再调一次模型，可能耗时几十秒。旧实现是一次性 JSON
   （阻塞到整轮跑完），前端点"同意"后长时间没有任何反馈，用户会以为无响应而
   重复点击 —— 第二次请求就落在"已无待审批项"上，于是弹窗消失并报"审批失败"。
2. 恢复阶段的工具调用流回前端后，才会挂在该条助手消息里，而不是被丢掉。

健壮性：
- 同一会话的审批互斥：恢复本身就是该会话的一轮**运行**（交给 `run_manager`），
  所以"是否已有恢复在跑"由运行管理器统一回答；恢复进行中的重复请求返回 409
  "正在处理"，而不是让第二次请求抢在第一次前面报"无待审批项"。
- 恢复动作跑在运行管理器里（与连接无关）：即使前端断流/刷新，审批结果也会写完，
  不会出现"点了同意但 graph 卡在中断态"；恢复途中刷新还能接回来看。
- **一个会话可以同时挂多个中断**：并行委派的子智能体各自触发审批时就会这样。
  恢复必须按 interrupt id 组映射，详见 `app/agent/approvals.py` 模块说明 ——
  这里踩过坑：只读第一个中断 + 扁平决策列表会被 langgraph 直接拒绝，
  表现是"点同意/拒绝都没反应"。
"""
import asyncio
import logging

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import StreamingResponse
from sqlalchemy.orm import Session as OrmSession

from app.agent import approvals, runtime
from app.agent.runner import run_manager
from app.api.deps import get_current_user, owned_session_or_404
from app.api.routes.chat_stream import SSE_HEADERS, sse
from app.core.crypto import scrub_secrets
from app.db.models import User
from app.db.session import get_db
from app.schemas.approval import ApprovalCheckResponse, ApprovalDecideRequest, ClarificationAnswerRequest

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/v1/approvals", tags=["approvals"])

#: session_id → {"busy": bool}。busy=True 表示该会话有一次恢复正在跑：
#: 它挡的是"请求刚进来、运行管理器还没接手"的那个窗口。
_state: dict[str, dict] = {}


def _config(session_id: str) -> dict:
    return {"configurable": {"thread_id": session_id}}


# ===== 人工介入开关（热插拔）=====
# 路由在 app/api/routes/hitl.py（前缀 /api/v1/hitl/config），刻意不放在本模块：
# 本模块的 /{session_id} 会把 "config" 当成会话 id 吃掉，造成 405 Method Not Allowed。


async def clear_pending_interrupt(session_id: str | None, user_id: str | None = None) -> list[str]:
    """把挂起的中断用 reject 唤醒（放弃待审批操作，让本轮正常收尾）。

    供人工介入开关调用：关闭审批时若某会话正挂着中断，不唤醒的话那半轮工具调用
    会一直悬着（而且**用户之后点"同意"会得到 409「无待审批项」** —— 因为关掉开关后
    那次重建可能让中断读不出来；现在图结构统一了，但"悬着的半轮"依然必须收尾）。

    目标会话 = 明确指定的那个 ∪ 进程内登记过"挂着中断"的会话。
    只清前端传来的那一个是不够的：前端只知道"自己内存里记过弹窗"的会话，
    刷新过 / 切过页面 / 关过标签页的会话它一无所知 —— 那些中断会留在库里，
    等下次重新打开人工介入时在弹窗里"复活"。

    `user_id` 用于**限定范围**。多租户下不带这个参数就等于"我关自己的审批，
    顺手把别人正在等的审批全 reject 了" —— 那不是安全问题（不泄漏数据），
    但是明确的跨用户干扰。传空表示进程级收尾（仅诊断/测试用）。

    多个会话**并发**收尾：每个恢复都会跑一次模型（reject 之后模型要接着把话说完），
    串行会把这些时间叠加成十几秒的"切换中…"。
    """
    targets: list[str] = []
    if session_id:
        targets.append(session_id)
    for sid in approvals.known_pending_sessions(user_id):
        if sid not in targets:
            targets.append(sid)
    if not targets:
        return []

    results = await asyncio.gather(
        *(_resume_pending(sid) for sid in targets), return_exceptions=True
    )

    cleared: list[str] = []
    for sid, payload in zip(targets, results):
        if payload == "ok":
            cleared.append(sid)
            approvals.forget_pending(sid)
        elif isinstance(payload, BaseException):
            logger.warning("[hitl] 清理会话 %s 的挂起中断失败: %s", sid, payload)
        elif payload is not None:
            logger.warning("[hitl] 清理会话 %s 的挂起中断失败: %s", sid, payload)
    if cleared:
        logger.info("[hitl] 已放弃挂起审批的会话: %s", cleared)
    return cleared


async def _resume_pending(session_id: str) -> str | None:
    """把该会话挂起的中断用 reject 唤醒；返回 "ok" / 错误文案 / None（本就无挂起）。

    归属不在这里校验：调用方（开关路由）已经限定在"当前用户的会话"范围内。
    这里用 `session_owner()` 拿到归属只是为了给 get_agent 定位工作区。
    """
    owner = runtime.session_owner(session_id)
    agent = await runtime.get_agent(session_id, user_id=owner)
    info = await approvals.read_pending_approval(agent, _config(session_id), user_id=owner)
    if not info.get("actions"):
        return None
    resume = _resume_payload(info, approve=False)
    try:
        # 走运行管理器：这本来就是该会话的一轮运行（状态诚实 + 可回放）。
        # 然后一直订阅到它收尾 —— 调用方（关闭人工介入开关）需要"中断确实已处理"。
        run = run_manager.start(session_id, runtime.resume_input(resume), user_id=owner)
        async for _event in run_manager.subscribe(session_id, since=0):
            pass
    except Exception as e:
        return scrub_secrets(f"{type(e).__name__}: {e}")
    remaining = await approvals.read_pending_approval(agent, _config(session_id), user_id=owner)
    return None if remaining.get("clarifications") else "ok"


def _resume_payload(info: dict, approve: bool):
    """按中断粒度构造恢复值。

    优先用**多中断映射**（一个会话可以同时挂多个中断，扁平列表会被 langgraph 拒绝）；
    只有在完全拿不到 interrupt id 时才退回旧的扁平决策列表。
    """
    resume = approvals.build_resume_map(info.get("interrupts"), approve=approve)
    if resume:
        return resume
    return approvals.build_decision(info.get("actions"), approve=approve)


@router.get("/{session_id}", response_model=ApprovalCheckResponse)
async def check_approval(
    session_id: str,
    user: User = Depends(get_current_user),
    db: OrmSession = Depends(get_db),
):
    """查看该会话当前是否有待审批项（含全部中断）。"""
    owned_session_or_404(db, session_id, user)
    agent = await runtime.get_agent(session_id, user_id=user.id, history_only=True)
    info = await approvals.read_pending_approval(agent, _config(session_id), user_id=user.id)
    return ApprovalCheckResponse(
        session_id=session_id,
        pending=info.get("pending", False),
        actions=info.get("actions") or [],
        interrupts=info.get("interrupts") or [],
        clarifications=info.get("clarifications") or [],
    )


@router.post("/{session_id}")
async def decide(
    session_id: str,
    req: ApprovalDecideRequest,
    user: User = Depends(get_current_user),
    db: OrmSession = Depends(get_db),
):
    """批准或拒绝当前待审批项，并以 SSE 流回恢复执行的过程与最终回复。"""
    owned_session_or_404(db, session_id, user)
    agent = await runtime.get_agent(session_id, user_id=user.id)
    info = await approvals.read_pending_approval(agent, _config(session_id), user_id=user.id)
    if not info.get("actions"):
        # 说清楚"为什么没有了"：前端据此**关闭弹窗并同步**，而不是把它当成一次
        # 含糊的失败（旧文案只说"可能已处理过"，前端于是提示"已提交过"却留着弹窗，
        # 用户会反复点同一个按钮）。
        raise HTTPException(
            status_code=409,
            detail="当前无待审批项（该审批可能已经处理过，或人工介入已切到「自动执行」）",
        )

    # 重复点击保护：只认「是否真的有恢复在跑」。
    # 正在恢复 → 409「正在处理中」（而不是让第二次请求抢在第一次前面，报出
    # "无待审批项"这种让人以为失败的结果）；恢复结束后即可重试。
    if (_state.get(session_id) or {}).get("busy") or run_manager.is_running(session_id):
        raise HTTPException(status_code=409, detail="审批已提交，正在处理中，请稍候…")

    approve = req.decision == "approve"
    resume = _resume_payload(info, approve=approve)
    # 展示用计数：本次决策覆盖多少个 action（多个中断时是所有中断的 action 之和）
    decided = sum(len(g.get("decisions") or []) for g in resume.values()) if isinstance(resume, dict) \
        else len(resume)

    try:
        run = run_manager.start(session_id, runtime.resume_input(resume), user_id=user.id)
    except RuntimeError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc

    async def _gen():
        # 标记"处理中"：这期间再来一次审批请求会被 409 挡掉。
        # （run_manager 那边也会挡：恢复本身就是该会话的一轮运行。）
        _state[session_id] = {"busy": True}
        try:
            yield sse({"type": "session", "session_id": session_id})
            yield sse({
                "type": "decision",
                "decision": "approve" if approve else "reject",
                "count": decided,
            })
            # since=起跑时的缓冲长度：只订阅**本次恢复**的增量。
            # 不回放缓冲 —— 那一轮在审批前已经堆了几千条事件，全量回放
            # 会把整个回合在前端重复渲染一遍。
            async for evt in run_manager.subscribe(session_id, since=0):
                yield sse(evt)
            yield "event: done\ndata: {}\n\n"
        finally:
            _state[session_id] = {"busy": False}

    return StreamingResponse(_gen(), media_type="text/event-stream", headers=SSE_HEADERS)


@router.post("/{session_id}/answer")
async def answer_question(
    session_id: str,
    req: ClarificationAnswerRequest,
    user: User = Depends(get_current_user),
    db: OrmSession = Depends(get_db),
):
    """Answer exactly one clarification; operation approvals remain untouched."""
    owned_session_or_404(db, session_id, user)
    if not req.cancelled and not req.answer.strip():
        raise HTTPException(status_code=422, detail="请填写回答或选择取消任务")
    if run_manager.is_running(session_id):
        raise HTTPException(status_code=409, detail="回答已提交，正在处理中，请稍候")
    agent = await runtime.get_agent(session_id, user_id=user.id)
    info = await approvals.read_pending_approval(agent, _config(session_id), user_id=user.id)
    if not any(question["id"] == req.interrupt_id for question in info.get("clarifications", [])):
        raise HTTPException(status_code=409, detail="当前无待回答问题（问题已处理或已失效）")
    # No await between checking the current interrupt and starting: duplicate requests cannot claim it twice.
    try:
        run = run_manager.start(session_id, runtime.resume_input({question["id"]: {
            "answer": req.answer.strip(), "cancelled": req.cancelled,
        } for question in info.get("clarifications", [])
            if req.cancelled or question["id"] == req.interrupt_id}), user_id=user.id)
    except RuntimeError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc

    async def events():
        yield sse({"type": "session", "session_id": session_id})
        yield sse({"type": "answer", "interrupt_id": req.interrupt_id, "cancelled": req.cancelled})
        async for event in run_manager.subscribe(session_id, since=0):
            yield sse(event)
        yield "event: done\ndata: {}\n\n"
    return StreamingResponse(events(), media_type="text/event-stream", headers=SSE_HEADERS)
