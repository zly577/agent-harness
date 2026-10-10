"""会话管理路由：列表 / 历史 / 重命名 / 删除 / 运行状态与停止（P15；P18 加归属隔离）。

## P18：每一个以 session_id 入参的端点，第一件事都是归属校验

统一走 `owned_session_or_404(db, session_id, user)`：不存在**或**不属于当前用户
一律 404（刻意不区分，避免被用来枚举别人的 session_id 是否存在）。

⚠️ 添加新端点时不要忘了这一步 —— 漏掉的后果是"任何人凭一个 session_id 就能读
别人的对话与文件"。`tests/test_p18_isolation.py` 会用两个账号交叉打这些端点，
漏了会红。
"""
from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session as OrmSession

from app.agent import runtime
from app.agent.runner import run_manager
from app.api.deps import get_current_user, owned_session_or_404
from app.api.routes.chat_stream import SSE_HEADERS, sse, stream_events
from app.db.models import Session as DbSession
from app.db.models import User
from app.db.session import get_db

router = APIRouter(prefix="/api/v1", tags=["sessions"])


class RenameRequest(BaseModel):
    title: str = Field(..., min_length=1, max_length=255, description="新标题")
    #: P18：会话可以记住模型供应商（可空 = 跟随用户/系统默认）。
    #: 用"未提供"与"显式置空"区分：None = 不改；空字符串 = 清除绑定。
    provider_id: str | None = Field(
        None, description="绑定的模型供应商 id；传空字符串表示清除绑定"
    )


def _serialize(s: DbSession) -> dict:
    return {
        "id": s.id,
        "title": s.title,
        "status": s.status,
        "provider_id": s.provider_id,
        "created_at": s.created_at.isoformat() if s.created_at else None,
        "updated_at": s.updated_at.isoformat() if s.updated_at else None,
    }


@router.get("/sessions")
def list_sessions(
    user: User = Depends(get_current_user),
    db: OrmSession = Depends(get_db),
):
    """返回**当前用户**的会话（按最近更新倒序），供前端侧边栏展示。

    P18：以前这里是 `db.query(DbSession).all()` —— 全表返回，任何登录用户都能
    看到所有人的会话列表（这正是本期要修的头号问题）。

    额外带上 `running`：该会话后端是否还有正在进行的回答。
    运行与页面无关（见 app/agent/runner.py），所以即使你切到了别的会话，
    侧边栏也能显示"那边还在输出"。
    """
    rows = (db.query(DbSession)
            .filter(DbSession.user_id == user.id)
            .order_by(DbSession.updated_at.desc())
            .all())
    out = []
    for s in rows:
        item = _serialize(s)
        item["running"] = run_manager.is_running(s.id)
        out.append(item)
    return out


@router.get("/sessions/{session_id}/messages")
async def get_session_messages(
    session_id: str,
    user: User = Depends(get_current_user),
    db: OrmSession = Depends(get_db),
):
    """读取某会话的消息历史（回放对话）。"""
    owned_session_or_404(db, session_id, user)
    return await runtime.load_history(session_id, user_id=user.id)


@router.patch("/sessions/{session_id}")
def rename_session(
    session_id: str,
    req: RenameRequest,
    user: User = Depends(get_current_user),
    db: OrmSession = Depends(get_db),
):
    """重命名会话标题（并可选地改变它绑定的模型供应商）。"""
    title = req.title.strip()
    if not title:
        raise HTTPException(status_code=422, detail="title cannot be empty")
    s = owned_session_or_404(db, session_id, user)

    s.title = title[:255]
    if req.provider_id is not None:
        s.provider_id = req.provider_id.strip() or None
    db.commit()
    return _serialize(s)


@router.get("/sessions/{session_id}/steps")
def get_session_steps(
    session_id: str,
    user: User = Depends(get_current_user),
    db: OrmSession = Depends(get_db),
):
    """任务看板的「当前步骤」：该会话的工具调用读模型（P20）。

    数据来自派生读模型 `task_steps`（由 `tool_call` 事件投影而来，见
    `app/db/projections.py`），每行带 `intent_label`（给用户看的一句话）
    与 `intent_source`（model / fallback / backfill）。

    `intent_label` 是**模型生成的文本**，前端渲染时必须转义 —— 这里原样返回，
    不做任何 HTML/Markdown 处理（把它当数据，不当标记）。
    """
    owned_session_or_404(db, session_id, user)
    from app.db.projections import list_steps

    return {"session_id": session_id, "steps": list_steps(db, session_id)}


@router.get("/sessions/{session_id}/run")
async def get_session_run(
    session_id: str,
    user: User = Depends(get_current_user),
    db: OrmSession = Depends(get_db),
):
    """查询该会话是否有正在进行的回答（刷新/切会话后据此决定要不要接回去）。

    返回 `{running, status, user_message, error, finished_at, ...}`。
    没有运行过、或已超过保留期时返回 `{"running": false, "status": "idle"}`。
    """
    owned_session_or_404(db, session_id, user)
    run = run_manager.current(session_id)
    if run is None:
        return {"session_id": session_id, "running": False, "status": "idle",
                "user_message": "", "error": "", "event_count": 0}
    return run.snapshot()


@router.get("/sessions/{session_id}/run/stream")
async def stream_session_run(
    session_id: str,
    user: User = Depends(get_current_user),
    db: OrmSession = Depends(get_db),
):
    """接回该会话正在进行的回答：先回放已产生的事件，再接上直播。

    这就是"刷新页面 / 关掉标签页再打开 / 切走再切回来"能继续看的入口 ——
    因为运行本身跑在后台（见 app/agent/runner.py），跟这个连接没有绑定关系。

    ⚠️ 归属校验必须在**返回 StreamingResponse 之前**做完。放进生成器里就晚了：
    状态码与响应头那时已经发出去，只能吐一个 error 事件 —— 前端看到的是
    "连接成功但什么都没发生"。这也是"A 不能接走 B 的 SSE 流"的唯一关口。
    """
    owned_session_or_404(db, session_id, user)

    async def _gen():
        if run_manager.current(session_id) is None:
            # 没有可接的运行：直接收尾，前端据此回到"空闲"状态
            yield sse({"type": "result", "content": "", "resumed": False})
            yield "event: done\ndata: {}\n\n"
            return
        yield sse({"type": "resumed", "session_id": session_id})
        async for chunk in stream_events(run_manager.subscribe(session_id)):
            yield chunk

    return StreamingResponse(_gen(), media_type="text/event-stream", headers=SSE_HEADERS)


@router.post("/sessions/{session_id}/stop")
async def stop_session_run(
    session_id: str,
    user: User = Depends(get_current_user),
    db: OrmSession = Depends(get_db),
):
    """**用户明确停止**本轮回答（这是除进程退出外唯一会中断运行的方式）。"""
    owned_session_or_404(db, session_id, user)
    stopped = await run_manager.stop_and_wait(session_id)
    if not stopped:
        # 没有在跑也不算错误：前端可能在"刚好跑完"的一瞬间点了停止
        return {"ok": True, "stopped": False, "detail": "该会话当前没有正在进行的回答"}
    return {"ok": True, "stopped": True}


@router.delete("/sessions/{session_id}")
def delete_session(
    session_id: str,
    user: User = Depends(get_current_user),
    db: OrmSession = Depends(get_db),
):
    """删除会话（业务元信息；checkpoint 历史保留在 SQLite 中）。"""
    s = owned_session_or_404(db, session_id, user)
    db.delete(s)
    db.commit()
    return {"ok": True}
