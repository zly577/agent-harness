"""SSE 流式对话路由（起改为"订阅运行"而不是"驱动运行"）。

POST /api/v1/chat/stream  body: {"message","session_id"}
返回 text/event-stream，每行 data: <json>。

## 关键变化：连接不再等于运行

以前这个端点的 SSE 生成器**就是**跑 agent 的那个协程，客户端一断（刷新/关页/
切网络）就被 Starlette 取消，回答随之消失。现在：

    run_manager.start(...)        # 后台跑，与请求无关
    run_manager.subscribe(...)    # 这里只是"订阅"，断线不影响运行

于是刷新页面、切会话、关标签页都不会打断回答；重新连上还能从头回放。

事件（chat/stream、run/stream 与 approvals 的恢复流共用同一套）：
  - {"type":"session","session_id":"..."} 会话ID
  - {"type":"token","content":"..."}      模型输出增量（只含主智能体）
  - {"type":"thinking","scope","delta"}   推理增量（思考中，让长推理不再黑屏）
  - {"type":"tool_call","id","name","args"} 工具调用（含参数）
  - {"type":"tool_result","id","name","content","ok","exit_code","reason","duration_ms"} 工具返回结果
    （后四个字段是后端判定好的成败/耗时，前端不要从 content 里猜）
  - {"type":"subagent_start","id","subagent_type","summary"} 子智能体开始（摘要）
  - {"type":"subagent_token","id","delta"} 子智能体产出增量
  - {"type":"subagent_thinking","id","delta"} 子智能体推理增量
  - {"type":"subagent_tool","id","name","args","result","status"} 子智能体自己的工具
  - {"type":"subagent_end","id","reason"}  子智能体结束（done/timeout）
  - {"type":"subagent_usage","id","input","output","total","reasoning"} 子智能体用量
  - {"type":"usage","input","output","total","scope"} 精确 token 用量（主智能体）
  - {"type":"approval","pending":true,...} 需要人工审批
  - {"type":"stopped","detail":"..."}      用户点了停止
  - {"type":"result","content":"..."}      最终回复
  - {"type":"error","detail":"..."}       错误
"""
import json
import logging

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import StreamingResponse
from sqlalchemy.orm import Session as OrmSession

from app.agent.attachments import prepare_content, supports_vision, save_refusal
from app.agent import runtime
from app.agent import providers as provider_svc
from app.agent.runner import run_manager
from app.agent.runner import to_sse_event  # noqa: F401  re-export：事件契约的归属地
from app.api.deps import get_current_user, owned_session_or_404
from app.core.crypto import scrub_secrets
from app.db.models import User
from app.db.session import get_db
from app.schemas.chat import ChatRequest

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/v1", tags=["chat-stream"])

#: SSE 响应头：关掉各级缓冲，保证事件逐条到达前端。
SSE_HEADERS = {
    "Cache-Control": "no-cache, no-transform",
    "Connection": "keep-alive",
    "X-Accel-Buffering": "no",
    "Content-Type": "text/event-stream; charset=utf-8",
}


def sse(obj: dict) -> str:
    """把事件字典编码成一条 SSE data 行。"""
    return f"data: {json.dumps(obj, ensure_ascii=False)}\n\n"


def apply_provider_choice(
    db: OrmSession,
    session_id: str,
    user: User,
    provider_id: str | None,
) -> None:
    """把本轮指定的模型供应商写回会话（含归属校验与 agent 重建）。

    放在这里（而不是 agent 层）是因为它要抛 HTTP 语义的错：
    用户传了一个不存在/别人的/已停用的 provider → 404，而不是静默用旧的
    （"我换了模型但它没换"比报错难查得多）。

    `chat.py`（非流式）也复用它 —— 两条入口必须行为一致，
    否则"脚本调的那条能用、界面这条不能用"这种问题会很难解释。
    """
    if provider_id is None:
        return
    row = provider_svc.get_owned_provider(db, provider_id, user.id)
    if row is None or not row.enabled:
        raise HTTPException(status_code=404, detail="provider not found or disabled")
    runtime.set_session_provider(session_id, provider_id)


async def stream_events(events):
    """把事件序列包成 SSE：异常转 error 事件，保证前端一定能收到收尾信号。"""
    try:
        async for evt in events:
            yield sse(evt)
        yield "event: done\ndata: {}\n\n"
    except Exception as e:  # 任何异常都要让前端知道，不能静默挂断连接
        logger.exception("[sse] stream failed")
        # 过一遍脱敏：错误信息回给前端，而 SDK 报错里可能带 api_key
        yield sse({"type": "error", "detail": scrub_secrets(f"{type(e).__name__}: {e}")})
        yield "event: done\ndata: {}\n\n"


async def stream_agent_events(events):
    """兼容旧调用：runtime 的 (kind, data) 流 → SSE（会把事件先映射成字典）。"""
    try:
        async for kind, data in events:
            yield sse(to_sse_event(kind, data))
        yield "event: done\ndata: {}\n\n"
    except Exception as e:  # noqa: BLE001
        logger.exception("[sse] stream failed")
        yield sse({"type": "error", "detail": scrub_secrets(f"{type(e).__name__}: {e}")})
        yield "event: done\ndata: {}\n\n"


@router.post("/chat/stream")
async def chat_stream(
    req: ChatRequest,
    user: User = Depends(get_current_user),
    db: OrmSession = Depends(get_db),
):
    """发一条消息并订阅这一轮的运行（运行本身在后台，不受本连接生死影响）。

      归属：没有 session_id 就建一个**归属当前用户**的；带了 session_id
    就先校验它属于当前用户（否则 404）。`user_id` 会随本轮运行一起被捕获
    （见 `run_manager.start`），供工作区/长期记忆定位。
    """
    content, has_images = prepare_content(req)

    # 会话解析：无 session_id 则新建；有则校验归属
    if req.session_id is None:
        workspace_id = req.workspace_id if req.workspace_id != "legacy" else None
        if workspace_id:
            from app.db.models import Workspace
            workspace = db.get(Workspace, workspace_id)
            if not workspace or workspace.user_id != user.id:
                raise HTTPException(status_code=404, detail="工作区不存在")
        session_id = runtime.create_session(user.id, runtime.session_title(req.message), workspace_id=workspace_id)
    else:
        row = owned_session_or_404(db, req.session_id, user)
        if row.title == "新会话":
            row.title = runtime.session_title(req.message)
            db.commit()
        session_id = req.session_id
        runtime.touch_session(session_id)

    # 本轮指定的供应商（可选）。先确认用户能用它，再写回会话 + 重建 agent。
    # 顺序很重要：**必须在起运行之前**，否则这一轮会先用旧模型跑起来。
    if req.provider_id is not None:
        apply_provider_choice(db, session_id, user, req.provider_id)

    # 同一会话不允许两轮并行：会同时改同一份 checkpoint
    if run_manager.is_running(session_id):
        raise HTTPException(
            status_code=409,
            detail="该会话已有一轮回答正在进行中。请等它结束，或先点停止。",
        )

    refusal = False
    if has_images:
        from app.agent.providers import resolve_provider
        cfg, _ = resolve_provider(db, user.id, runtime.session_context(session_id)[1])
        refusal = not supports_vision(cfg)

    async def _gen():
        # 先推 session 事件（前端据此记录 session_id）
        yield sse({"type": "session", "session_id": session_id})
        if refusal:
            reply = await save_refusal(session_id, req.message + "\n" + "\n".join("[附件：" + a.name + "]" for a in req.attachments), user.id)
            yield sse({"type": "result", "content": reply})
            yield "event: done\ndata: {}\n\n"
            return
        try:
            # 只在后台起运行 —— 这个生成器只负责"订阅"。
            # user_id 在这里（请求时刻）捕获后传下去：后台 task 里没有"当前用户"。
            run_manager.start(
                session_id,
                {"messages": [{"role": "user", "content": content}]},
                user_message=req.message,
                user_id=user.id,
            )
        except RuntimeError as e:
            # 竞态：刚好在两次检查之间起来了另一轮
            yield sse({"type": "error", "detail": str(e)})
            yield "event: done\ndata: {}\n\n"
            return
        async for chunk in stream_events(run_manager.subscribe(session_id)):
            yield chunk

    return StreamingResponse(_gen(), media_type="text/event-stream", headers=SSE_HEADERS)
