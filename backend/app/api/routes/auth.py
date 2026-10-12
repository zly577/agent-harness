"""认证路由：注册 / 登录 / 当前用户 / 改密码。

前缀 `/api/v1/auth`。**这个 router 不经过业务聚合 router**，所以不受"默认拒绝"
的全局依赖约束（见 `app/api/routes/__init__.py`）—— 否则就登不进去了。

## 三个安全细节

**① 登录失败不区分"邮箱不存在"和"密码错误"。**
统一回 `邮箱或密码不正确`。区分开来等于给攻击者一个免费的账号枚举接口。

**② 邮箱不存在时也做一次哈希校验。**
argon2id 校验很慢（这正是它的价值），但代价是：不存在的账号会**立即**返回，
于是响应时间差暴露了账号是否存在（几百毫秒 vs 几毫秒，肉眼可见）。
所以 `_DUMMY_HASH` 让"不存在"也走一遍完整校验。

**③ 未登录 / 密码错误都返回 401，且不把底层异常写进响应。**
argon2 的 `VerificationError` 之类只在服务端日志里，不出去。
"""
from __future__ import annotations

import logging
import uuid
import secrets
from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session as OrmSession

from app.api.deps import get_current_user
from app.core.config import settings
from app.core.security import (
    create_access_token,
    hash_password,
    needs_rehash,
    verify_password,
)
from app.db.models import User, RegistrationCode
from app.core.registration_mail import send_registration_code
from app.db.session import get_db
from app.schemas.auth import (
    ChangePasswordRequest,
    LoginRequest,
    RegisterRequest,
    SendRegistrationCodeRequest,
    TokenResponse,
    UserOut,
)

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/v1/auth", tags=["auth"])

#: 用于"账号不存在时也跑一次哈希校验"的占位哈希（见模块头注释②）。
#: 值本身没有意义，只要是一个合法的 argon2id 串、且不会匹配任何真实密码即可。
_DUMMY_HASH = "$argon2id$v=19$m=65536,t=3,p=4$c29tZXNhbHRzb21lc2FsdA$" + "0" * 43


def _issue_token(user: User) -> TokenResponse:
    """签发 token 并组装响应。"""
    token, expires_in = create_access_token(user.id, role=user.role or "user")
    return TokenResponse(access_token=token, expires_in=expires_in, user=UserOut.of(user))


@router.post("/register/code")
def request_registration_code(req: SendRegistrationCodeRequest, db: OrmSession = Depends(get_db)):
    if not settings.allow_registration:
        raise HTTPException(403, "暂未开放注册，请联系管理员")
    if db.scalar(select(User).where(User.email == req.email)):
        raise HTTPException(409, "该邮箱已被注册，请直接登录")
    now = datetime.now(timezone.utc).replace(tzinfo=None)
    challenge = db.scalar(select(RegistrationCode).where(RegistrationCode.email == req.email).with_for_update())
    if challenge and (now - challenge.sent_at).total_seconds() < 60:
        raise HTTPException(429, "发送太频繁，请 60 秒后再试")
    code = f"{secrets.randbelow(1000000):06d}"
    if challenge is None:
        challenge = RegistrationCode(email=req.email)
        db.add(challenge)
    challenge.code_hash = hash_password(code)
    challenge.sent_at = now
    challenge.expires_at = now + timedelta(minutes=10)
    challenge.attempts = 0
    try:
        db.flush()
    except IntegrityError:
        db.rollback()
        raise HTTPException(429, "验证码正在发送，请稍后再试") from None
    try:
        send_registration_code(req.email, code)
    except Exception:
        db.rollback()
        logger.warning("[auth] Registration email delivery failed")
        raise HTTPException(503, "验证码暂时无法发送，请稍后再试或联系管理员") from None
    db.commit()
    return {"detail": "验证码已发送，请查看邮箱，10 分钟内有效", "retry_after": 60}


@router.post("/register", response_model=UserOut, status_code=201)
def register(req: RegisterRequest, db: OrmSession = Depends(get_db)):
    """自助注册。

    返回 201 + 用户信息（**不**直接发 token）：注册与登录是两个语义，
    由前端在注册成功后自动调一次登录，用户无感，但接口契约更干净。

    Raises:
        HTTPException 403: 服务端关闭了自助注册。
        HTTPException 409: 邮箱已被占用。
    """
    if not settings.allow_registration:
        raise HTTPException(
            status_code=403,
            detail="本服务已关闭自助注册，请联系管理员创建账号",
        )

    exists = db.scalar(select(User).where(User.email == req.email).limit(1))
    if exists is not None:
        raise HTTPException(status_code=409, detail="该邮箱已被注册")

    challenge = db.scalar(select(RegistrationCode).where(RegistrationCode.email == req.email).with_for_update())
    now = datetime.now(timezone.utc).replace(tzinfo=None)
    if not challenge or challenge.expires_at <= now:
        raise HTTPException(400, "验证码已过期或尚未发送，请重新获取")
    if challenge.attempts >= 5:
        raise HTTPException(429, "验证码错误次数过多，请重新获取")
    if not verify_password(req.verification_code, challenge.code_hash):
        challenge.attempts += 1
        db.commit()
        raise HTTPException(400, "验证码不正确，请检查后重新输入")

    display_name = (req.display_name or "").strip() or req.email.split("@")[0]
    user = User(
        id=uuid.uuid4().hex,
        email=req.email,  # 已在 schema 里归一为小写
        password_hash=hash_password(req.password),
        display_name=display_name[:128],
        # 自助注册一律是普通用户；管理员只能由引导流程或已有管理员创建
        role="user",
        is_active=True,
    )
    db.add(user)
    db.delete(challenge)
    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        raise HTTPException(409, "该邮箱已被注册，请直接登录") from None
    db.refresh(user)
    logger.info("[auth] 新用户注册: %s", user.email)
    return UserOut.of(user)


@router.post("/login", response_model=TokenResponse)
def login(req: LoginRequest, db: OrmSession = Depends(get_db)):
    """登录，返回 access token。

    Raises:
        HTTPException 401: 邮箱或密码不正确 / 账号被禁用。
    """
    user = db.scalar(select(User).where(User.email == req.email).limit(1))

    if user is None:
        # 见模块头注释②：走一次无意义的校验，把"账号不存在"的响应时间拉平
        verify_password(req.password, _DUMMY_HASH)
        raise HTTPException(status_code=401, detail="邮箱或密码不正确")

    if not verify_password(req.password, user.password_hash):
        raise HTTPException(status_code=401, detail="邮箱或密码不正确")

    if not user.is_active:
        raise HTTPException(status_code=401, detail="账号已被禁用，请联系管理员")

    # 哈希参数升级（例如以后调大 argon2 的内存开销）时顺手换掉，
    # 用户完全无感 —— 只在能成功登录的前提下做，所以不需要额外授权。
    if needs_rehash(user.password_hash):
        try:
            user.password_hash = hash_password(req.password)
            db.commit()
            logger.info("[auth] 已为用户 %s 升级密码哈希参数", user.email)
        except Exception as e:  # noqa: BLE001 升级失败不该让登录失败
            db.rollback()
            logger.warning("[auth] 密码哈希升级失败（忽略）: %s", e)

    logger.info("[auth] 用户登录: %s", user.email)
    return _issue_token(user)


@router.get("/me", response_model=UserOut)
def me(user: User = Depends(get_current_user)):
    """当前登录用户。前端启动时用它校验本地 token 是否还有效。"""
    return UserOut.of(user)


@router.post("/change-password")
def change_password(
    req: ChangePasswordRequest,
    user: User = Depends(get_current_user),
    db: OrmSession = Depends(get_db),
):
    """修改自己的密码。

    为什么本期就加（需求里没列）：引导管理员的密码只影响**创建**时刻，
    users 表一旦有行，改 `.env` 的 `BOOTSTRAP_ADMIN_PASSWORD` 就不会再生效 ——
    没有这个接口，第一个管理员的密码就永远改不掉了。

    零新依赖、逻辑极短，属于"不做就不完整"的部分。
    """
    if req.old_password == req.new_password:
        raise HTTPException(status_code=422, detail="新密码不能与原密码相同")
    if not verify_password(req.old_password, user.password_hash):
        raise HTTPException(status_code=401, detail="原密码不正确")

    user.password_hash = hash_password(req.new_password)
    db.commit()
    logger.info("[auth] 用户 %s 修改了密码", user.email)
    return {"ok": True, "detail": "密码已更新，请用新密码重新登录"}


def user_count(db: OrmSession) -> int:
    """用户总数（辅助/调试用）。"""
    return db.scalar(select(func.count()).select_from(User)) or 0


__all__ = ["router", "user_count"]
