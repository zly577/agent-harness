"""密码哈希与 access token。

## 密码用 argon2id，不是 bcrypt

项目原始需求写的是 bcrypt，实现时改成 **argon2id**，两个实际理由：

1. `argon2-cffi` 在本机 `agent_env` 里**已经装好了**（25.1.0，是 `cryptography` 生态的常客），
   而 bcrypt 需要新装，还带一个众所周知的坑：`passlib` 与 bcrypt 4.x 的版本探测不兼容
   （`AttributeError: module 'bcrypt' has no attribute '__about__'`）。
2. argon2id 是当前密码哈希的推荐算法（2015 PHC 竞赛优胜），抗 GPU/ASIC 比 bcrypt 更强。

哈希串自带参数（`$argon2id$v=19$m=65536,t=3,p=4$...`），所以以后调参数也不用迁移数据，
`verify_password` 照常能验；`needs_rehash` 用来判断要不要在用户下次登录时顺手升级。

## token 用 HS256 的 JWT

payload：`{sub: user_id, role, typ: "access", iat, exp}`。
只做 access token，**不做 refresh token** —— 这个前端是单文件 SPA（无 vue-router），
access 有效期可配（默认 24h），过期一律回登录页重新登录，比维护 refresh 轮换简单可靠。
将来要加，`decode_token` 里已经用 `typ` 做了区分位。

## 密钥来源

`settings.jwt_secret` 为空时走 `app/core/secrets_store.resolve_secret` 的开发兜底
（落盘到 `backend/data/.dev_secrets.json`，重启不变）。见该模块的说明。
"""
from __future__ import annotations

import logging
import secrets
from datetime import datetime, timedelta, timezone
from typing import Any

import jwt
from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerificationError, VerifyMismatchError

from app.core.config import settings
from app.core.secrets_store import resolve_secret

logger = logging.getLogger(__name__)

#: 全局复用（PasswordHasher 无状态、线程安全；每次构造会重新算参数，没必要）
_hasher = PasswordHasher()

#: token 类型标记，为将来加 refresh token 留位
TOKEN_TYPE_ACCESS = "access"


class TokenError(Exception):
    """token 无效/过期。路由层统一转 401（不要让它冒成 500）。"""


# ============================================================================
# 密码
# ============================================================================


def hash_password(password: str) -> str:
    """把明文口令哈希成 argon2id 串（自带随机 salt）。"""
    return _hasher.hash(password)


def verify_password(password: str, password_hash: str) -> bool:
    """校验口令。

    **任何**异常都返回 False —— 库里存着空串/半截哈希（比如手工改过库）时，
    这里必须给出"验证失败"而不是抛异常，否则前端拿到的是 500 而不是 401。
    """
    if not password or not password_hash:
        return False
    try:
        return bool(_hasher.verify(password_hash, password))
    except (VerifyMismatchError, VerificationError, InvalidHashError):
        return False
    except Exception as e:  # noqa: BLE001 兜底：绝不让校验路径抛出去
        logger.warning("[security] 口令校验异常（按失败处理）: %s: %s", type(e).__name__, e)
        return False


def needs_rehash(password_hash: str) -> bool:
    """该哈希是否需要用当前参数重新生成（参数升级后，用户下次登录时顺手换掉）。"""
    if not password_hash:
        return True
    try:
        return bool(_hasher.check_needs_rehash(password_hash))
    except Exception:  # noqa: BLE001 无法判断的哈希一律当成"需要重建"
        return True


# ============================================================================
# JWT
# ============================================================================


def _secret() -> str:
    """签名密钥（配置优先，开发环境落盘兜底）。"""
    return resolve_secret(
        settings.jwt_secret,
        "jwt_secret",
        lambda: secrets.token_urlsafe(48),
    )


def create_access_token(
    user_id: str,
    *,
    role: str = "user",
    expires_minutes: int | None = None,
) -> tuple[str, int]:
    """签发 access token。

    Returns:
        `(token, expires_in_seconds)` —— 秒数直接回给前端做倒计时/预判，
        避免前端自己解析 JWT 的过期时间（少一处能写错的逻辑）。
    """
    minutes = (
        int(expires_minutes)
        if expires_minutes is not None
        else int(settings.access_token_expire_minutes)
    )
    now = datetime.now(timezone.utc)
    expire = now + timedelta(minutes=minutes)
    payload: dict[str, Any] = {
        "sub": str(user_id),
        "role": role,
        "typ": TOKEN_TYPE_ACCESS,
        "iat": int(now.timestamp()),
        "exp": int(expire.timestamp()),
    }
    token = jwt.encode(payload, _secret(), algorithm=settings.jwt_algorithm)
    return token, minutes * 60


def decode_token(token: str) -> dict:
    """解析并校验 token。

    Returns:
        payload dict（含 `sub` / `role`）。

    Raises:
        TokenError: 过期、签名不对、结构损坏、类型不是 access。
    """
    if not token or not token.strip():
        raise TokenError("缺少凭证")
    try:
        payload = jwt.decode(
            token,
            _secret(),
            algorithms=[settings.jwt_algorithm],
            options={"require": ["exp", "sub"]},
        )
    except jwt.ExpiredSignatureError as e:
        raise TokenError("登录已过期，请重新登录") from e
    except jwt.InvalidTokenError as e:
        # 签名错/结构错/缺 exp 都落这里，**不把底层信息回给前端**（避免提示性泄漏）
        raise TokenError("登录凭证无效，请重新登录") from e

    if payload.get("typ") != TOKEN_TYPE_ACCESS:
        raise TokenError("登录凭证无效，请重新登录")
    return payload


__all__ = [
    "TOKEN_TYPE_ACCESS",
    "TokenError",
    "create_access_token",
    "decode_token",
    "hash_password",
    "needs_rehash",
    "verify_password",
]
