"""MCP 工具源管理路由。

前缀 `/api/v1/mcp`。全部需要登录（聚合 router 已挂 `get_current_user`），
并且**每一行都带 user_id 归属**：别人的 MCP 既看不见也改不了（越权一律 404）。

## 路由声明顺序

`/status`、`/reload`、`/servers/test` 这类**固定路径**必须写在 `/{server_id}` 之前。
FastAPI 按注册顺序匹配，一旦 `/{server_id}` 先注册，`POST /servers/test` 就会被
当成 `server_id="test"` 吃掉（`hitl.py` 的模块注释里记着这个坑：当时表现为
405 Method Not Allowed，排查了很久）。

## 改完配置怎么生效

MCP 工具是在 `runtime.get_agent()` **构建 agent 时**加载的，而 agent 有缓存。
所以任何改动都要 `invalidate_agents(user_id=...)`，否则用户的感受是
"保存成功了但工具没出现"。本模块里所有写操作都自动做这件事。
"""
from __future__ import annotations

import logging
import uuid

from fastapi import APIRouter, Depends, HTTPException
from pydantic import ValidationError
from sqlalchemy.orm import Session as OrmSession

from app.agent import mcp_registry as registry
from app.agent.mcp import (
    McpServerConfig,
    adapters_missing_hint,
    probe_mcp_server,
)
from app.agent.runtime import invalidate_agents
from app.api.deps import get_current_user
from app.core.config import settings
from app.db.models import McpServer, User
from app.db.session import get_db
from app.schemas.mcp import (
    McpServerCreate,
    McpServerOut,
    McpServerUpdate,
    McpStatusOut,
    McpTestRequest,
    McpTestResult,
)

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/v1/mcp", tags=["mcp"])


# ============================================================================
# 序列化 / 工具
# ============================================================================


def _serialize(row: McpServer) -> McpServerOut:
    return McpServerOut(
        id=row.id,
        name=row.name or "",
        transport=row.transport or "streamable_http",
        url=row.url or "",
        command=row.command or "",
        args=registry.args_of(row),
        cwd=row.cwd or "",
        enabled=bool(row.enabled),
        header_keys=registry.header_keys(row),
        has_headers=registry.has_headers(row),
        has_env=registry.has_env(row),
        created_at=getattr(row, "created_at", None),
        updated_at=getattr(row, "updated_at", None),
    )


def _bad_request(e: Exception) -> HTTPException:
    """把配置校验失败翻成 400 的可读中文。

    `McpServerConfig` / pydantic 抛的是英文 ValueError，直接回给用户没人看得懂
    （"stdio 类型必须有 command" 这种才是人话）。
    """
    if isinstance(e, ValidationError):
        first = e.errors()[0] if e.errors() else {}
        msg = str(first.get("msg") or "参数不合法")
        loc = ".".join(str(x) for x in first.get("loc") or [])
        return HTTPException(status_code=422, detail=f"{loc}: {msg}" if loc else msg)
    return HTTPException(status_code=400, detail=str(e))


def _reload(user_id: str) -> list[str]:
    """让改动立刻生效：丢掉该用户所有已缓存的 agent。

    返回被丢掉的会话键（给调用方报个数）。
    """
    dropped = invalidate_agents(user_id=user_id)
    logger.info("[mcp] 已失效 %d 个 agent 缓存（user=%s）", len(dropped), user_id)
    return dropped


# ============================================================================
# 状态
# ============================================================================


@router.get("/status", response_model=McpStatusOut)
def mcp_status(
    user: User = Depends(get_current_user),
    db: OrmSession = Depends(get_db),
):
    """MCP 总开关 / 依赖是否装好 / 我配了几个。

    `enabled=False` 时数据库里的配置**都在**，只是不加载 ——
    前端据此提示"总开关没开"，否则用户会以为自己配错了。
    """
    from app.agent.mcp import _has_adapters

    return McpStatusOut(
        enabled=bool(settings.mcp_enabled),
        adapters_installed=_has_adapters(),
        server_count=len(registry.list_rows(db, user.id)),
        connect_timeout_seconds=settings.mcp_connect_timeout_seconds,
    )


# ============================================================================
# 列表 / 增删改
# ============================================================================


@router.get("/servers", response_model=list[McpServerOut])
def list_servers(
    user: User = Depends(get_current_user),
    db: OrmSession = Depends(get_db),
):
    """列出**我自己的** MCP 工具源。headers/env 只回键名，值永不回显。"""
    return [_serialize(r) for r in registry.list_rows(db, user.id)]


@router.post("/servers", response_model=McpServerOut, status_code=201)
def create_server(
    req: McpServerCreate,
    user: User = Depends(get_current_user),
    db: OrmSession = Depends(get_db),
):
    """新增一个 MCP 工具源（**一律建自己私有的**）。"""
    try:
        cfg = req.to_config()
    except (ValueError, ValidationError) as e:
        raise _bad_request(e) from e

    # 同一用户下重名会让"哪个生效"变成玄学（合并时按 name 去重）
    dup = [r for r in registry.list_rows(db, user.id) if (r.name or "") == cfg.name]
    if dup:
        raise HTTPException(status_code=409, detail=f"你已经有一个叫「{cfg.name}」的 MCP 工具源了")

    row = McpServer(id=uuid.uuid4().hex, user_id=user.id, enabled=True)
    registry.apply_config(row, cfg)
    db.add(row)
    db.commit()
    db.refresh(row)
    logger.info("[mcp] 新增 %s（owner=%s, transport=%s）", row.name, user.id, row.transport)
    _reload(user.id)
    return _serialize(row)


@router.put("/servers/{server_id}", response_model=McpServerOut)
def update_server(
    server_id: str,
    req: McpServerUpdate,
    user: User = Depends(get_current_user),
    db: OrmSession = Depends(get_db),
):
    """修改。未提供的字段不动；`headers`/`env` 传 `{}` 才是清空。"""
    row = registry.owned_row(db, server_id, user.id)
    if row is None:
        raise HTTPException(status_code=404, detail="mcp server not found")

    try:
        current = registry.config_from_row(row)
    except ValueError as e:
        # 密文解不开（FERNET_KEY 换过）—— 说清楚该怎么做，别抛底层异常
        raise HTTPException(status_code=400, detail=str(e)) from e

    try:
        cfg = req.merged_config(current)
    except (ValueError, ValidationError) as e:
        raise _bad_request(e) from e

    if cfg.name != (row.name or "") and any(
        (r.name or "") == cfg.name for r in registry.list_rows(db, user.id)
    ):
        raise HTTPException(status_code=409, detail=f"你已经有一个叫「{cfg.name}」的 MCP 工具源了")

    registry.apply_config(row, cfg)
    db.commit()
    db.refresh(row)
    _reload(user.id)
    return _serialize(row)


@router.delete("/servers/{server_id}")
def delete_server(
    server_id: str,
    user: User = Depends(get_current_user),
    db: OrmSession = Depends(get_db),
):
    """删除。已经加载到某个 agent 里的工具会在下次重建 agent 时消失（reload 触发）。"""
    row = registry.owned_row(db, server_id, user.id)
    if row is None:
        raise HTTPException(status_code=404, detail="mcp server not found")
    db.delete(row)
    db.commit()
    _reload(user.id)
    return {"ok": True}


# ============================================================================
# 连通性测试
# ============================================================================


@router.post("/servers/test", response_model=McpTestResult)
async def test_temp_config(
    req: McpTestRequest,
    user: User = Depends(get_current_user),
):
    """用**临时配置**测试（先测后存，不落库）。"""
    try:
        cfg: McpServerConfig = McpTestRequest.to_config(req)
    except (ValueError, ValidationError) as e:
        raise _bad_request(e) from e
    result = await probe_mcp_server(cfg, timeout=req.timeout)
    return McpTestResult(**result)


@router.post("/servers/{server_id}/test", response_model=McpTestResult)
async def test_saved_server(
    server_id: str,
    user: User = Depends(get_current_user),
    db: OrmSession = Depends(get_db),
):
    """用**已保存**的配置测试（解密后用一次，明文不外泄）。"""
    from app.agent.mcp import _has_adapters

    if not _has_adapters():
        return McpTestResult(ok=False, message=adapters_missing_hint(), tools=[], detail="")

    row = registry.owned_row(db, server_id, user.id)
    if row is None:
        raise HTTPException(status_code=404, detail="mcp server not found")
    try:
        cfg = registry.config_from_row(row)
    except ValueError as e:
        return McpTestResult(ok=False, message=str(e), tools=[], detail="")

    result = await probe_mcp_server(cfg)
    return McpTestResult(**result)


# ============================================================================
# 热重载
# ============================================================================


@router.post("/reload")
def reload_mcp(
    user: User = Depends(get_current_user),
    db: OrmSession = Depends(get_db),
):
    """丢掉我的 agent 缓存，让刚改的配置立刻生效。

    正常路径下（增删改）已经自动做了；这个端点留给"改了 YAML 文件"这类
    后端侧的手工操作 —— 否则只能重启进程。

    `mcp_enabled=False` 时依然可调用（清缓存本身无害），但会返回一句提示，
    免得用户 reload 完发现工具还是没出现。
    """
    dropped = _reload(user.id)
    hint = "" if settings.mcp_enabled else "注意：MCP 总开关 MCP_ENABLED 当前是关闭的，配置不会加载"
    return {"ok": True, "dropped": len(dropped), "hint": hint}


__all__ = ["router"]
