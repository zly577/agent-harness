"""认证接口的请求/响应模型。

## 为什么不用 `pydantic.EmailStr`

`EmailStr` 需要额外装 `email-validator`（本机未装），而且它的校验规则会**拒绝
`admin@local` 这种没有顶级域的名字** —— 恰恰是引导管理员的默认登录名。
在这个项目里 email 只是"登录标识"，不是信件投递地址，所以用一条刻意的宽松规则：

    <非空且无空白与 @ 的本地部分>@<非空无空白且无 @ 的域名部分>

换成严格校验的收益（挡住 `a@b`）远小于它带来的"自己的默认账号登不进去"的麻烦。
真要收紧，改 `_validate_email` 一处即可，注册/登录/引导三条路径都走它。
"""
from __future__ import annotations

import re
from datetime import datetime

from pydantic import BaseModel, Field, field_validator

#: 一条 @，两侧都非空、无空白。故意不要求顶级域（见模块头注释）
_EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+$")

#: 口令长度上下限。
#: 上限不是"安全"考虑而是**成本**考虑：argon2id 按输入长度做哈希，
#: 一个超长口令（或恶意构造的 1MB 字符串）会明显拖慢 CPU。
PASSWORD_MIN = 8
PASSWORD_MAX = 128


def _validate_email(value: str) -> str:
    """归一化 + 宽松校验。存库与比对统一用**小写**，避免 `A@x.com` 与 `a@x.com` 变成两个账号。"""
    email = (value or "").strip().lower()
    if not email or len(email) > 255 or not _EMAIL_RE.match(email):
        raise ValueError("邮箱格式不正确")
    return email


class RegisterRequest(BaseModel):
    """自助注册。是否开放由配置 `ALLOW_REGISTRATION` 控制。"""

    email: str = Field(..., description="登录邮箱（大小写不敏感）")
    password: str = Field(
        ..., min_length=PASSWORD_MIN, max_length=PASSWORD_MAX,
        description=f"口令，{PASSWORD_MIN}~{PASSWORD_MAX} 位",
    )
    display_name: str = Field("", max_length=128, description="展示名，可空（默认取邮箱 @ 前的部分）")

    @field_validator("email")
    @classmethod
    def _email(cls, v: str) -> str:
        return _validate_email(v)


class LoginRequest(BaseModel):
    """登录。"""

    email: str = Field(..., description="登录邮箱")
    password: str = Field(..., min_length=1, max_length=PASSWORD_MAX, description="口令")

    @field_validator("email")
    @classmethod
    def _email(cls, v: str) -> str:
        return _validate_email(v)


class UserOut(BaseModel):
    """对外的用户信息。**绝不包含 password_hash**。"""

    id: str
    email: str
    display_name: str
    role: str
    is_active: bool
    created_at: datetime | None = None

    @classmethod
    def of(cls, user) -> "UserOut":
        """从 ORM 对象构造（避免开 `from_attributes` 而意外带出更多字段）。"""
        return cls(
            id=user.id,
            email=user.email,
            display_name=user.display_name or "",
            role=user.role or "user",
            is_active=bool(user.is_active),
            created_at=getattr(user, "created_at", None),
        )


class TokenResponse(BaseModel):
    """登录成功的响应。

    `expires_in` 是秒数 —— 直接给前端，省得它自己去解 JWT 的 `exp`
    （少一处能写错、且时区容易搞错的地方）。
    """

    access_token: str
    token_type: str = "bearer"
    expires_in: int
    user: UserOut


class ChangePasswordRequest(BaseModel):
    """改口令。

    ⚠️ 必须是**请求体**，不能写成函数参数（那会变成 query string）：
    口令出现在 URL 里会被 uvicorn 的 access log、反向代理日志、浏览器历史
    统统记下来 —— 一个"改口令"接口把口令写进日志，比不改还糟。
    """

    old_password: str = Field(..., min_length=1, max_length=PASSWORD_MAX, description="原口令")
    new_password: str = Field(
        ..., min_length=PASSWORD_MIN, max_length=PASSWORD_MAX,
        description=f"新口令，{PASSWORD_MIN}~{PASSWORD_MAX} 位",
    )


__all__ = [
    "PASSWORD_MAX",
    "PASSWORD_MIN",
    "ChangePasswordRequest",
    "LoginRequest",
    "RegisterRequest",
    "TokenResponse",
    "UserOut",
]
