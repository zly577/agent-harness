"""人工介入（HITL）热插拔开关路由。

单独成模块、单独前缀（`/api/v1/hitl`），是为了**彻底避开路径歧义**：
如果挂在 `/api/v1/approvals/config`，它会和 `/api/v1/approvals/{session_id}` 抢匹配
（谁先注册谁赢）。一旦顺序错了或者在旧进程上跑，PUT 就会落到只接受 POST 的
`/{session_id}` 上，报 405 Method Not Allowed —— 这正是线上遇到的问题。

- GET  /api/v1/hitl/config[?session_id=]   查看开关
- PUT  /api/v1/hitl/config                 热插拔：{"enabled": true|false, "session_id": 可选}

## P18：开关有三个作用域，默认只动自己那一层

| 请求 | 作用域 | 谁能用 |
|------|--------|--------|
| 带 `session_id` | 只影响该会话 | 会话的归属者 |
| 不带 `session_id` | 改**全局默认** | **仅 admin** |

为什么全局那层要收紧：以前"不带 session_id"就是全局切换，任何用户点一下页头的
开关都会改掉所有人的默认值；而且关闭时还会把**所有**挂起的中断一起 reject 掉
（见 `approvals.clear_pending_interrupt` 的说明）。多租户下这属于跨用户干扰。
"""
import logging

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session as OrmSession

from app.agent import permissions, runtime
from app.api.deps import get_current_user, owned_session_or_404
from app.db.models import User
from app.db.session import get_db
from app.schemas.approval import HitlConfigRequest, HitlConfigResponse

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/v1/hitl", tags=["hitl"])


@router.get("/config", response_model=HitlConfigResponse)
async def get_hitl_config(
    session_id: str | None = None,
    user: User = Depends(get_current_user),
    db: OrmSession = Depends(get_db),
):
    """查看人工介入开关。

    - 传 `session_id`：看该会话的有效值（会话 → 用户 → 全局）
    - 不传：看自己的有效值（用户 → 全局）
    """
    if session_id:
        owned_session_or_404(db, session_id, user)
    return HitlConfigResponse(**permissions.hitl_state(session_id, user.id))


@router.put("/config", response_model=HitlConfigResponse)
async def set_hitl_config(
    req: HitlConfigRequest,
    user: User = Depends(get_current_user),
    db: OrmSession = Depends(get_db),
):
    """热插拔人工介入：开启后敏感写操作才需要人工同意，关闭则全程自动执行。

    权限是 **编译期** 输入（deepagents 有 interrupt 权限才挂 HITL 中间件），
    所以切换时必须丢掉缓存的 Agent，下次对话用新权限重建 —— 这就是"热插拔"。

    作用域规则见 `HitlConfigRequest` 的说明（会话 / 自己 / 全局三层）。
    """
    from app.api.routes.approvals import clear_pending_interrupt

    if req.session_id:
        # 会话级：必须是自己的会话
        owned_session_or_404(db, req.session_id, user)
        scope = "session"
    elif req.scope == "global":
        # 全局默认：仅管理员（普通用户请求这一层会被拒绝，而不是悄悄降级 ——
        # "以为改了所有人其实只改了自己"比直接报错更难排查）
        if (user.role or "").lower() != "admin":
            raise HTTPException(
                status_code=403,
                detail="修改全局默认需要管理员权限；请只对自己生效（scope=user）",
            )
        scope = "global"
    else:
        # 用户级：只影响自己（前端页头那个开关走的就是这条）
        scope = "user"

    cleared: list[str] = []
    if not req.enabled:
        # 关闭前先把悬着的中断收尾，否则那半轮工具调用会一直挂在中断态。
        # P18：只收尾**自己的**会话 —— 不带这个限制就是"我关自己的审批，
        # 顺手把别人正在等的审批全 reject 了"。
        cleared = await clear_pending_interrupt(req.session_id, user_id=user.id)

    if scope == "session":
        permissions.set_hitl(req.enabled, req.session_id, user.id)
    elif scope == "user":
        permissions.set_hitl(req.enabled, None, user.id)
    else:
        permissions.set_hitl(req.enabled, None, None)

    # 权限是编译期输入：全局改动影响所有人 → 清空全部缓存；
    # 用户级只清这个用户的会话；会话级只清那一个会话。
    if scope == "global":
        dropped = runtime.invalidate_agents()
    elif scope == "user":
        dropped = runtime.invalidate_agents(user_id=user.id)
    else:
        dropped = runtime.invalidate_agents(req.session_id)

    state = permissions.hitl_state(req.session_id, user.id)
    logger.info(
        "[hitl] 开关=%s scope=%s 操作者=%s 重建缓存=%s 清理挂起=%s",
        state["enabled"], state["scope"], user.email, dropped, cleared,
    )
    return HitlConfigResponse(**state, rebuilt=bool(dropped), cleared=cleared)
