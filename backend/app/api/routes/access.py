"""权限模式热插拔路由（P21）。

单独成模块、单独前缀（`/api/v1/access`），理由与 `hitl.py` 一样：**避开路径歧义**。
如果把配置端点挂在别的资源下，很容易被 `/{some_id}` 这类动态段抢匹配（谁先注册谁赢），
一旦落到旧进程上就会报 405，排查成本远高于多开一个前缀。

- GET  /api/v1/access/config[?session_id=]   查看当前权限模式
- PUT  /api/v1/access/config                热插拔：{"mode": "sandbox"|"full", "session_id"?: ...}

## 为什么要重建 Agent

权限模式决定 `create_deep_agent(backend=...)`（沙箱 vs 本机 shell）与
`permissions=...`（要不要审批本机访问）两个**编译期**参数 —— 和 HITL 开关同一套机制，
所以切换后必须 `runtime.invalidate_agents(...)`，下次对话才用新模式重建。

## 作用域

| 请求 | 作用域 | 谁能用 |
|------|--------|--------|
| 带 `session_id` | 只影响该会话 | 会话的归属者 |
| 不带 `session_id` | 改**自己的**默认值 | 任何登录用户 |
| 不带 + `scope="global"` | 改全局默认 | **仅 admin** |

全局那一层收紧的原因：这是"要不要在本机无隔离地跑命令"的开关，
任何用户都能改全局等于所有人都被别人的一次点击影响。
"""
import logging

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session as OrmSession

from app.agent import access, runtime
from app.api.deps import get_current_user, owned_session_or_404
from app.db.models import User
from app.db.session import get_db
from app.schemas.access import AccessConfigRequest, AccessConfigResponse

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/v1/access", tags=["access"])


@router.get("/config", response_model=AccessConfigResponse)
async def get_access_config(
    session_id: str | None = None,
    user: User = Depends(get_current_user),
    db: OrmSession = Depends(get_db),
):
    """查看权限模式。

    - 传 `session_id`：看该会话的有效值（会话 → 用户 → 全局）
    - 不传：看自己的有效值（用户 → 全局）
    """
    if session_id:
        owned_session_or_404(db, session_id, user)
    return AccessConfigResponse(**access.access_state(session_id, user.id))


@router.put("/config", response_model=AccessConfigResponse)
async def set_access_config(
    req: AccessConfigRequest,
    user: User = Depends(get_current_user),
    db: OrmSession = Depends(get_db),
):
    """热插拔权限模式（默认权限（沙箱内） / 完全访问）。

    作用域规则见 `AccessConfigRequest` 的说明。切到 **完全访问** 时会先把挂起的审批
    收尾（拒绝），理由与关闭 HITL 时相同：审批门已经被撤掉，那些挂起的中断再"复活"
    只会让用户困惑（点同意 → 409「无待审批项」）。
    """
    from app.api.routes.approvals import clear_pending_interrupt

    # 非法模式返回 400（附可选值），而不是让它静默落到默认值 ——
    # "切了完全访问但其实是写错了"是最难自己发现的一类问题。
    try:
        mode = access.normalize_mode(req.mode)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e

    if req.session_id:
        owned_session_or_404(db, req.session_id, user)
        scope = "session"
    elif req.scope == "global":
        if (user.role or "").lower() != "admin":
            raise HTTPException(
                status_code=403,
                detail="修改全局默认需要管理员权限；请只对自己生效（scope=user）",
            )
        scope = "global"
    else:
        scope = "user"

    cleared: list[str] = []
    if mode == access.MODE_FULL:
        # 完全访问 = 不再需要任何审批 → 把悬着的中断收尾（只收尾自己的会话）。
        cleared = await clear_pending_interrupt(req.session_id, user_id=user.id)

    if scope == "session":
        access.set_access_mode(mode, req.session_id, user.id)
    elif scope == "user":
        access.set_access_mode(mode, None, user.id)
    else:
        access.set_access_mode(mode, None, None)

    # 模式是编译期输入：全局改动影响所有人 → 清空全部缓存；
    # 用户级只清这个用户的会话；会话级只清那一个会话。
    if scope == "global":
        dropped = runtime.invalidate_agents()
    elif scope == "user":
        dropped = runtime.invalidate_agents(user_id=user.id)
    else:
        dropped = runtime.invalidate_agents(req.session_id)

    state = access.access_state(req.session_id, user.id)
    logger.info(
        "[access] 模式=%s(%s) scope=%s 操作者=%s 重建缓存=%s 清理挂起=%s",
        state["mode"], state["label"], state["scope"], user.email, dropped, cleared,
    )
    return AccessConfigResponse(**state, rebuilt=bool(dropped), cleared=cleared)
