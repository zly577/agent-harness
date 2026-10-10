"""模型供应商管理路由（P18）。

前缀 `/api/v1/llm`。全部需要登录；**自己的**配置只有自己能动，
**系统级**（`user_id IS NULL`）只有 admin 能动。

## 密钥永远不会被读出来

列表/详情只回 `api_key_hint`（掩码）—— 它**落库时就算好存下来了**，
所以"看一眼供应商列表"这个动作不会解密任何密钥、也不会让明文进入进程内存。

## 路由声明顺序

`/available` 与 `/test` 这类**固定路径**必须写在 `/{provider_id}` 之前。
FastAPI 按注册顺序匹配，一旦 `/{provider_id}` 先注册，`POST /test` 就会被
当成 `provider_id="test"` 吃掉（`app/api/routes/hitl.py` 的模块注释里记着这个坑：
当时表现为 405 Method Not Allowed，排查了很久）。
"""
from __future__ import annotations

import json
import logging
import uuid

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select
from sqlalchemy.orm import Session as OrmSession

from app.agent import providers as provider_svc
from app.agent.model import DEFAULT_BASE_URLS, PROTOCOL_OPENAI, ProviderConfig
from app.api.deps import get_current_user
from app.core.crypto import encrypt_secret, mask_secret
from app.db.models import LlmProvider, User
from app.db.session import get_db
from app.schemas.llm import (
    ModelListResponse,
    ProviderCreate,
    ProviderOut,
    ProviderTestRequest,
    ProviderTestResult,
    ProviderUpdate,
)

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/v1/llm", tags=["llm"])


# ============================================================================
# 序列化
# ============================================================================


def _serialize(row: LlmProvider) -> ProviderOut:
    return ProviderOut(
        id=row.id,
        name=row.name or "",
        protocol=row.protocol or PROTOCOL_OPENAI,
        base_url=provider_svc.effective_base_url(row),
        model=row.model or "",
        parameters=provider_svc.parse_parameters(row.parameters),
        is_default=bool(row.is_default),
        enabled=bool(row.enabled),
        is_system=row.user_id is None,
        api_key_hint=row.api_key_hint or "",
        has_api_key=bool(row.api_key_encrypted),
        created_at=getattr(row, "created_at", None),
        updated_at=getattr(row, "updated_at", None),
    )


def _visible_rows(db: OrmSession, user: User) -> list[LlmProvider]:
    """当前用户可见的供应商：**只有他自己私有的**。

    2026-09-18 收紧：以前是"系统级 + 自己的"。使用方要求"每个用户自行配置模型，
    否则没有模型可用"，所以系统级那一层从解析与界面里一起退出了
    （见 `app/agent/providers.py` 模块头注释）。表结构仍保留 `user_id IS NULL`
    的能力，将来要恢复共享供应商只需改回这一处 + 解析顺序那一处。
    """
    return list(db.scalars(
        select(LlmProvider)
        .where(LlmProvider.user_id == user.id)
        .order_by(LlmProvider.created_at.asc())
    ))


def _writable_or_404(db: OrmSession, provider_id: str, user: User) -> LlmProvider:
    """取出可写的供应商行，否则 404 / 403。

    - 不存在 或 是别人的 → **404**（与其它资源一致，不泄漏存在性）
    - 是系统级但不是 admin → **403**（这个是"你看得到但不能改"，说清楚更有用）
    """
    row = db.get(LlmProvider, provider_id)
    if row is None:
        raise HTTPException(status_code=404, detail="provider not found")
    if row.user_id is None:
        if (user.role or "").lower() != "admin":
            raise HTTPException(
                status_code=403,
                detail="这是系统级供应商（所有用户共享），只有管理员可以修改",
            )
        return row
    if row.user_id != user.id:
        raise HTTPException(status_code=404, detail="provider not found")
    return row


def _clear_other_defaults(db: OrmSession, owner_id: str | None, keep_id: str) -> None:
    """把同一 owner 下其它行的 is_default 清掉。

    MySQL 无法用唯一索引表达"每 owner 至多一个 true"（部分索引/条件唯一），
    所以在应用层用事务保证。
    """
    stmt = select(LlmProvider).where(LlmProvider.is_default.is_(True),
                                     LlmProvider.id != keep_id)
    stmt = stmt.where(LlmProvider.user_id.is_(None) if owner_id is None
                      else LlmProvider.user_id == owner_id)
    for other in db.scalars(stmt):
        other.is_default = False


# ============================================================================
# 列表 / 选择器
# ============================================================================


@router.get("/providers", response_model=list[ProviderOut])
def list_providers(
    user: User = Depends(get_current_user),
    db: OrmSession = Depends(get_db),
):
    """列出可用供应商（系统级 + 自己的）。**api_key 只回掩码。**"""
    return [_serialize(r) for r in _visible_rows(db, user)]


@router.get("/providers/available", response_model=list[ProviderOut])
def list_available(
    user: User = Depends(get_current_user),
    db: OrmSession = Depends(get_db),
):
    """供对话区「模型选择器」用的轻量列表（只要启用的）。

    与 `/providers` 的差别只在过滤：这个给普通用户挑模型用，
    所以把停用的排除掉，省得前端各自再过滤一遍（各自实现就会出现不一致）。
    """
    return [_serialize(r) for r in _visible_rows(db, user) if r.enabled]


# ============================================================================
# 增删改
# ============================================================================


@router.post("/providers", response_model=ProviderOut, status_code=201)
def create_provider(
    req: ProviderCreate,
    user: User = Depends(get_current_user),
    db: OrmSession = Depends(get_db),
):
    """新增供应商。**一律建当前用户私有的**。

    不再支持 `scope="system"`：系统级供应商已退出解析（见模块头注释），
    再允许创建只会造出"库里有一条、但谁也选不到"的死配置。
    """
    owner_id: str | None = user.id

    row = LlmProvider(
        id=uuid.uuid4().hex,
        user_id=owner_id,
        name=(req.name or "").strip() or req.model,
        protocol=req.protocol,
        base_url=req.base_url,
        api_key_encrypted=encrypt_secret(req.api_key) if req.api_key else "",
        api_key_hint=mask_secret(req.api_key),
        model=req.model.strip(),
        parameters=json.dumps(req.parameters or {}, ensure_ascii=False),
        is_default=False,
        enabled=req.enabled,
    )
    db.add(row)

    # 第一条（且没明确说不要）= 自动设为默认，否则用户加完还得再点一次"设为默认"
    # 才可能用得上 —— 而"加完没生效"是最常见的困惑。
    if req.is_default or not db.scalar(
        select(LlmProvider).where(
            LlmProvider.user_id.is_(None) if owner_id is None
            else LlmProvider.user_id == owner_id
        ).limit(1)
    ):
        row.is_default = True
    db.flush()
    if row.is_default:
        _clear_other_defaults(db, owner_id, row.id)

    db.commit()
    db.refresh(row)
    logger.info("[llm] 新增供应商 %s（owner=%s, protocol=%s）",
                row.model, owner_id or "system", row.protocol)
    return _serialize(row)


@router.put("/providers/{provider_id}", response_model=ProviderOut)
def update_provider(
    provider_id: str,
    req: ProviderUpdate,
    user: User = Depends(get_current_user),
    db: OrmSession = Depends(get_db),
):
    """修改供应商。未提供的字段不动；`api_key` 留空 = 保留原密钥。"""
    row = _writable_or_404(db, provider_id, user)

    if req.name is not None:
        row.name = req.name.strip()
    if req.protocol is not None:
        row.protocol = req.protocol
    if req.base_url is not None:
        row.base_url = req.base_url
    if req.model is not None:
        row.model = req.model.strip()
    if req.parameters is not None:
        row.parameters = json.dumps(req.parameters, ensure_ascii=False)
    if req.enabled is not None:
        row.enabled = req.enabled
    if req.api_key:
        # 只在新值非空时覆盖：这样前端不必为了改个名字把明文密钥回填一遍
        row.api_key_encrypted = encrypt_secret(req.api_key)
        row.api_key_hint = mask_secret(req.api_key)

    if not (row.model or "").strip():
        raise HTTPException(status_code=422, detail="model 不能为空")

    db.commit()
    db.refresh(row)
    return _serialize(row)


@router.delete("/providers/{provider_id}")
def delete_provider(
    provider_id: str,
    user: User = Depends(get_current_user),
    db: OrmSession = Depends(get_db),
):
    """删除供应商。

    如果它有会话正绑着（`sessions.provider_id`），那些会话会在下次对话时
    自动回退到用户/系统默认 —— 由 `providers.resolve_provider` 的"会话绑定失效
    就回退"逻辑兜住，所以这里不需要拦。
    """
    row = _writable_or_404(db, provider_id, user)
    db.delete(row)
    db.commit()
    return {"ok": True}


@router.post("/providers/{provider_id}/set-default", response_model=ProviderOut)
def set_default(
    provider_id: str,
    user: User = Depends(get_current_user),
    db: OrmSession = Depends(get_db),
):
    """设为默认。

    - 自己的供应商 → 设为**我的**默认（清掉自己其它行的 default）
    - 系统级供应商 → 需要 admin（它设的是**全局**默认；普通用户想用它，
      只要不给自己设默认就会自动回退到系统默认）
    """
    row = db.get(LlmProvider, provider_id)
    if row is None:
        raise HTTPException(status_code=404, detail="provider not found")

    if row.user_id is None:
        if (user.role or "").lower() != "admin":
            raise HTTPException(
                status_code=403,
                detail=(
                    "这是系统级供应商，它的默认设置是全局的、仅管理员可改。"
                    "你不需要设置它：只要没有自己的默认供应商，就会自动使用系统默认。"
                ),
            )
    elif row.user_id != user.id:
        raise HTTPException(status_code=404, detail="provider not found")

    row.is_default = True
    db.flush()
    _clear_other_defaults(db, row.user_id, row.id)
    db.commit()
    db.refresh(row)
    return _serialize(row)


# ============================================================================
# 连通性测试
# ============================================================================


@router.post("/providers/test", response_model=ProviderTestResult)
async def test_temp_config(
    req: ProviderTestRequest,
    user: User = Depends(get_current_user),
):
    """用**临时配置**测试连通性（"先测后存"，不落库）。

    声明在 `/{provider_id}/test` 之前只是习惯 —— 两者路径形状不同不会互相吃掉，
    但保持"固定路径在前"的一致规则，省得以后加端点时踩坑。
    """
    cfg = ProviderConfig(
        protocol=req.protocol,
        model=req.model.strip(),
        api_key=req.api_key or "",
        base_url=req.base_url,
        parameters=req.parameters or {},
        name="(临时配置)",
    )
    result = await provider_svc.probe_provider(cfg)
    return ProviderTestResult(**result)


@router.post("/providers/{provider_id}/test", response_model=ProviderTestResult)
async def test_saved_provider(
    provider_id: str,
    user: User = Depends(get_current_user),
    db: OrmSession = Depends(get_db),
):
    """用**已保存**的配置测试连通性（解密后用一次，明文不外泄）。"""
    # 只读可见性：自己的 或 系统级（系统级普通用户也能测 —— 测一下不影响别人）
    row = provider_svc.get_owned_provider(db, provider_id, user.id)
    if row is None:
        raise HTTPException(status_code=404, detail="provider not found")
    try:
        cfg = provider_svc.config_from_row(row)
    except ValueError as e:
        # 密钥解不开（FERNET_KEY 换过）—— 给出可操作的中文提示
        return ProviderTestResult(ok=False, message=str(e), detail="")

    result = await provider_svc.probe_provider(cfg)
    return ProviderTestResult(**result)


@router.get("/providers/{provider_id}/models", response_model=ModelListResponse)
async def list_models(
    provider_id: str,
    user: User = Depends(get_current_user),
    db: OrmSession = Depends(get_db),
):
    """拉取供应商的模型列表（`GET {base_url}/models`）。

    **失败不报错**：很多兼容实现没有这个端点（Ollama 有、部分内网网关没有），
    所以按需求 §4.3"失败不报错，返回空并提示手填"处理。
    """
    row = provider_svc.get_owned_provider(db, provider_id, user.id)
    if row is None:
        raise HTTPException(status_code=404, detail="provider not found")
    try:
        cfg = provider_svc.config_from_row(row)
    except ValueError as e:
        return ModelListResponse(ok=False, models=[], message=str(e))

    return await fetch_models(cfg)


async def fetch_models(cfg: ProviderConfig) -> ModelListResponse:
    """查 `{base_url}/models`（OpenAI 兼容端点）。"""
    import httpx

    base = (cfg.base_url or DEFAULT_BASE_URLS.get(cfg.protocol, "")).rstrip("/")
    if not base:
        return ModelListResponse(ok=False, models=[], message="未配置 base_url，无法拉取模型列表")
    url = f"{base}/models"

    headers = {"Authorization": f"Bearer {cfg.api_key}"}
    if cfg.protocol == "anthropic":
        # Anthropic 的模型列表用的是它自己的两个头
        headers = {"x-api-key": cfg.api_key, "anthropic-version": "2023-06-01"}

    try:
        async with httpx.AsyncClient(timeout=15) as client:
            resp = await client.get(url, headers=headers)
        if resp.status_code >= 400:
            return ModelListResponse(
                ok=False, models=[],
                message=f"该供应商未提供模型列表接口（HTTP {resp.status_code}），请手动填写模型名",
            )
        data = resp.json()
        items = data.get("data") if isinstance(data, dict) else None
        if not isinstance(items, list):
            return ModelListResponse(ok=False, models=[], message="返回格式不认识，请手动填写模型名")
        names = sorted({
            str(i.get("id") or i.get("name") or "").strip()
            for i in items if isinstance(i, dict)
        } - {""})
        return ModelListResponse(ok=True, models=names, message=f"共 {len(names)} 个模型")
    except Exception as e:  # noqa: BLE001 拉不到列表不影响使用
        return ModelListResponse(
            ok=False, models=[],
            message=f"拉取模型列表失败（{type(e).__name__}），请手动填写模型名",
        )


__all__ = ["fetch_models", "router"]
