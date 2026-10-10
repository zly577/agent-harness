"""FastAPI 依赖：当前用户与资源归属校验（P18）。

## 归属校验为什么是**辅助函数**而不是 `Depends`

两种写法都能用，这里选函数有具体理由：

- 路由需要**同一个** `db` 会话去做后续操作（改标题、删会话）。写成
  `Depends(get_owned_session)` 的话，依赖里查一遍、路由里再查一遍，
  要么重复查询，要么把一个 ORM 对象跨依赖传递（还要小心它属于哪个 session）。
- 函数形态一眼能看出"哪一行做了校验"，`grep owned_session_or_404` 就能列出
  所有需要归属校验的地方 —— 这正是本期最容易漏的地方，可检索性比语法糖重要。

## 越权为什么返回 404 而不是 403

403 等于承认"这个资源存在，只是你没权限"，攻击者可以用它枚举出别人的
session_id 是否存在。404 让"不存在"和"不是你的"变得无法区分。
（需求 §3.3.2 明确要求。）
"""
from __future__ import annotations

from fastapi import Depends, Header, HTTPException
from sqlalchemy.orm import Session as OrmSession

from app.core.security import TokenError, decode_token
from app.db.models import Session as DbSession
from app.db.models import User
from app.db.session import get_db

#: 资源不可见时的统一响应文案。
#: 沿用 P2 起的英文写法（`test_api_routes.py` 断言的就是这个字符串），
#: 同时保证"不存在"与"不属于你"返回完全相同的内容。
NOT_FOUND_DETAIL = "session not found"


def _unauthorized(detail: str) -> HTTPException:
    """401 统一构造：带 `WWW-Authenticate: Bearer`，符合 RFC 6750。"""
    return HTTPException(
        status_code=401, detail=detail, headers={"WWW-Authenticate": "Bearer"}
    )


def extract_bearer_token(authorization: str | None) -> str:
    """从 `Authorization: Bearer <token>` 里取出 token。

    用 `partition(" ")` 而不是 `split(" ")`：`split` 遇到多余空格会多切出一段，
    而 token 里不允许有空格，`partition` 的语义正好。

    Raises:
        HTTPException 401: 缺失/格式不对。
    """
    raw = (authorization or "").strip()
    if not raw:
        raise _unauthorized("未登录，请先登录")
    scheme, sep, token = raw.partition(" ")
    if not sep or scheme.lower() != "bearer" or not token.strip():
        raise _unauthorized("登录凭证格式不正确，请重新登录")
    return token.strip()


def get_current_user(
    authorization: str | None = Header(default=None),
    db: OrmSession = Depends(get_db),
) -> User:
    """解析当前登录用户。任何问题（缺头、过期、签名错、被禁用）一律 401。

    关于 `db`：FastAPI 对同一依赖函数会做**请求内缓存**，所以这里和路由里的
    `Depends(get_db)` 拿到的是同一个 Session —— 不会为一个请求开两条连接。
    """
    token = extract_bearer_token(authorization)
    try:
        payload = decode_token(token)
    except TokenError as e:
        # decode_token 给出的已经是可以直接展示的中文（不泄漏底层原因）
        raise _unauthorized(str(e)) from e

    user_id = str(payload.get("sub") or "")
    user = db.get(User, user_id) if user_id else None
    if user is None:
        # token 签名有效但用户被删了 —— 也当 401（不是 404：这跟具体资源无关）
        raise _unauthorized("账号不存在，请重新登录")
    if not user.is_active:
        raise _unauthorized("账号已被禁用，请联系管理员")
    return user


def get_current_admin(user: User = Depends(get_current_user)) -> User:
    """要求管理员。用于全局开关、系统级供应商、技能定义写入等系统级能力。"""
    if (user.role or "").lower() != "admin":
        raise HTTPException(status_code=403, detail="该操作需要管理员权限")
    return user


def owned_session_or_404(db: OrmSession, session_id: str, user: User) -> DbSession:
    """取出属于 `user` 的会话，否则 404。

    这是**所有以 session_id 入参的路由的第一道动作**。集中在这里，
    是为了让"忘记校验归属"这件事在代码评审里一眼可见（而不是散落在
    十几个路由里各自手写一遍 `if s.user_id != user.id`）。

    Raises:
        HTTPException 404: 会话不存在 **或** 不属于当前用户。
    """
    if not session_id:
        raise HTTPException(status_code=404, detail=NOT_FOUND_DETAIL)
    session = db.get(DbSession, session_id)
    if session is None or session.user_id != user.id:
        # 刻意不区分这两种情况、也不记日志：把"别人的 id"和"不存在的 id"
        # 记成不同的事件，等于在日志里留下一个可被探测的信道。
        raise HTTPException(status_code=404, detail=NOT_FOUND_DETAIL)
    return session


__all__ = [
    "NOT_FOUND_DETAIL",
    "extract_bearer_token",
    "get_current_admin",
    "get_current_user",
    "owned_session_or_404",
]
