"""API 路由聚合（安全默认 = 拒绝）。

## 默认拒绝是怎么做到的

给聚合 router 挂一个 **router 级依赖** `get_current_user`：

    api_router = APIRouter(dependencies=[Depends(get_current_user)])

于是凡是 `include_router(api_router)` 进来的业务路由，**无论新旧、无论作者记不记得
加鉴权**，都自动要求登录。这比"每个路由自己写 `Depends(get_current_user)`"可靠得多：
需求 §3.2.5 担心的"新路由漏加鉴权"在结构上就不可能发生。

## 例外

`auth` router（登录/注册）**不在**这里挂载 —— 它在 `app/main.py` 里单独
`include_router`。否则登录接口本身也要先登录，死锁。
"""
from fastapi import APIRouter, Depends

from app.api.deps import get_current_user
from app.api.routes import (
    access,
    approvals,
    chat,
    chat_stream,
    files,
    hitl,
    llm,
    mcp,
    sessions,
    skills,
)

#: 业务路由聚合点。**所有**在这里注册的路由都要求已登录。
api_router = APIRouter(dependencies=[Depends(get_current_user)])

api_router.include_router(chat.router)
api_router.include_router(chat_stream.router)
api_router.include_router(sessions.router)
api_router.include_router(llm.router)
# MCP 工具源管理（按用户隔离）
api_router.include_router(mcp.router)
api_router.include_router(skills.router)
api_router.include_router(approvals.router)
api_router.include_router(hitl.router)
# 权限模式（默认权限（沙箱内）/ 完全访问）
api_router.include_router(access.router)
api_router.include_router(files.router)

__all__ = ["api_router"]
