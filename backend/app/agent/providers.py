"""供应商解析与连通性探测（P18）。

## 解析顺序（2026-09-18 收紧：**只用你自己的**）

    会话绑定的 provider_id  →  用户的默认供应商  →  ❌ 明确报错

**不再回退到系统级供应商，也不回退到 `.env` 的引导配置。**
这是使用方的明确要求："每个用户需要自行去设置模型，否则就没有模型可供使用"。

为什么要连后端也一起收紧（只改前端选择器是不够的）：
前端不列出来只是"界面看不到"，任何人仍然可以直接调 `POST /api/v1/chat`
（不带 provider_id）让后端走系统级那条 —— 于是"必须自己配模型"这条策略
在真实边界上根本不成立。**策略要落在解析这一层**，界面只是它的呈现。

最后一步**必须**给出可操作的中文提示（"去设置里加一个"），而不是让框架异常
自己冒出来 —— 用户看到的应该是"我该做什么"，不是一串 pydantic 校验栈。

## 密钥的明文边界

`config_from_row()` 是**唯一**的解密入口。它返回的 `ProviderConfig.api_key` 是明文，
生命周期只到 `build_llm()` 构造出模型实例为止。因此：

- 日志/报错一律用 `ProviderConfig.describe()`（不含密钥）；
- 探测失败的原因要过一遍 `crypto.scrub_secrets()`，因为供应商 SDK 报错时
  可能把请求头或 URL 带进 message；
- 列表接口根本不解密 —— 它读的是落库时就算好的 `api_key_hint` 掩码。

## 一个供应商 = 一个模型

需求允许"一个供应商多个模型"（`models` JSON），本期先做单模型（§4.1 也说了
"至少先支持单模型字段"）。多模型的自然做法是同一 base_url + 不同 model 建多条记录 ——
现在的表结构直接支持，不需要改 schema。
"""
from __future__ import annotations

import json
import logging
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session as OrmSession

from app.agent.model import (
    DEFAULT_BASE_URLS,
    PROTOCOL_ANTHROPIC,
    PROTOCOL_OPENAI,
    ProviderConfig,
    build_llm,
)
from app.core.crypto import decrypt_secret, scrub_secrets
from app.db.models import LlmProvider

logger = logging.getLogger(__name__)


class ProviderNotConfigured(RuntimeError):
    """当前用户名下没有任何可用供应商。消息本身就是给用户看的中文提示。"""

    def __init__(self, message: str | None = None) -> None:
        super().__init__(message or (
            "你还没有配置模型，无法使用。请点右上角「模型」新增一个供应商"
            "（填协议、base_url、api_key、模型名），并「测试连接」通过后再对话。"
        ))


# ============================================================================
# ORM ↔ 配置对象
# ============================================================================


def parse_parameters(raw: str | None) -> dict[str, Any]:
    """`llm_providers.parameters`（JSON 字符串）→ dict。坏数据返回空 dict。"""
    text = (raw or "").strip()
    if not text:
        return {}
    try:
        data = json.loads(text)
        return data if isinstance(data, dict) else {}
    except Exception:  # noqa: BLE001 手工改坏的 JSON 不该让整条链路起不来
        logger.warning("[providers] parameters 不是合法 JSON，按空处理: %r", text[:80])
        return {}


def config_from_row(row: LlmProvider) -> ProviderConfig:
    """ORM 行 → `ProviderConfig`（**在这里解密 api_key**）。"""
    return ProviderConfig(
        protocol=row.protocol or PROTOCOL_OPENAI,
        model=row.model or "",
        api_key=decrypt_secret(row.api_key_encrypted) if row.api_key_encrypted else "",
        base_url=(row.base_url or "").strip() or None,
        parameters=parse_parameters(row.parameters),
        name=row.name or "",
        provider_id=row.id,
        is_system=row.user_id is None,
        dialect="auto",
    )


def effective_base_url(row: LlmProvider) -> str:
    """展示用：留空时给出该协议的官方默认地址。"""
    return (row.base_url or "").strip() or DEFAULT_BASE_URLS.get(
        row.protocol or PROTOCOL_OPENAI, ""
    )


# ============================================================================
# 解析
# ============================================================================


def _pick_default(db: OrmSession, user_id: str | None) -> LlmProvider | None:
    """取某 owner 的默认供应商（没有标 default 就退回最早创建的那条启用的）。

    ⚠️ 只用于**用户自己的**（`user_id` 非空）。系统级那一层已经不再参与解析，
    见模块头注释。
    """
    if not user_id:
        return None
    stmt = (select(LlmProvider)
            .where(LlmProvider.enabled.is_(True), LlmProvider.user_id == user_id))
    rows = list(db.scalars(stmt.order_by(LlmProvider.created_at.asc())))
    if not rows:
        return None
    for r in rows:
        if r.is_default:
            return r
    return rows[0]


def get_owned_provider(db: OrmSession, provider_id: str, user_id: str) -> LlmProvider | None:
    """按 id 取一个**属于该用户**的供应商。

    2026-09-18 收紧：不再把系统级（`user_id IS NULL`）算作"可读/可用"。
    解析、绑定会话、测试连接、拉模型列表都走这里，语义必须一致 ——
    否则会出现"选择器里看不到、但 API 能绑上"的缝。
    """
    if not user_id:
        return None
    row = db.get(LlmProvider, provider_id)
    if row is None or row.user_id != user_id:
        return None
    return row


def resolve_provider(
    db: OrmSession,
    user_id: str | None,
    session_provider_id: str | None = None,
) -> tuple[ProviderConfig, str]:
    """解析出本次该用哪个供应商。

    Returns:
        `(配置, 来源说明)`。来源说明用于日志（"会话绑定 / 用户默认"）。

    Raises:
        ProviderNotConfigured: 该用户名下没有任何可用供应商。消息已是给用户看的中文。
    """
    # 1) 会话绑定（必须是**他自己的**：以前这里不校验归属，等于开了个后门 ——
    #    直接把别人的 provider_id 填进来就能用别人的密钥）
    if session_provider_id:
        row = get_owned_provider(db, session_provider_id, user_id or "")
        if row is not None and row.enabled:
            try:
                return config_from_row(row), "会话绑定"
            except ValueError as e:
                # 密钥解不开（FERNET_KEY 换过）不该让整轮对话 500 —— 退到自己的默认，
                # 并把原因说清楚（用户能在供应商列表里重新填一次 key）。
                logger.warning("[providers] 会话绑定的供应商无法解密，回退默认: %s", e)
        elif row is not None:
            logger.warning("[providers] 会话绑定的供应商已停用，回退默认: %s", row.id)
        else:
            logger.warning("[providers] 会话绑定的供应商不存在或不属于该用户，回退默认: %s",
                           session_provider_id)

    # 2) 用户自己的默认
    row = _pick_default(db, user_id)
    if row is not None:
        try:
            return config_from_row(row), "用户默认"
        except ValueError as e:
            logger.warning("[providers] 用户默认供应商无法解密: %s", e)

    # ❌ 到此为止：**不回退**系统级，也不回退 .env。
    # 这是"每个用户必须自己配模型"这条策略的落点（见模块头注释）。
    raise ProviderNotConfigured()


def resolve_for_agent(db: OrmSession, user_id: str | None, session_provider_id: str | None) -> ProviderConfig:
    """`resolve_provider` 的便捷形态：只要配置，并记一条来源日志。"""
    cfg, source = resolve_provider(db, user_id, session_provider_id)
    logger.info("[providers] 本次使用供应商：%s（来源：%s）", cfg.describe(), source)
    return cfg


# ============================================================================
# 系统级供应商：不再创建，改为「一次性收编」
# ============================================================================


def migrate_system_providers(db: OrmSession, owner_user_id: str | None) -> int:
    """把遗留的**系统级**供应商收编给 `owner_user_id`（改成他的私有供应商）。

    背景：P18 早期版本支持"系统级共享供应商"——启动时用 `.env` 的 DeepSeek 配置
    自动建一条（所有用户可用）。2026-09-18 使用方要求改成"每个用户必须自己配模型"，
    系统级这一层随之退出解析。

    那条已经存在的记录有两种处理方式，这里选**收编**而不是删除：
    - 删掉 → 使用方（以及任何老部署）会突然"没有模型可用"，还得重新填一遍密钥；
    - 收编 → 密钥原样保留（密文不动），归属改成引导管理员，**无缝继续可用**。

    幂等：收编之后就没有 `user_id IS NULL` 的行了，第二次执行是空操作。
    若该 owner 名下已有一条同模型的供应商，则保留既有那条、把系统级这条删掉，
    避免出现两条一模一样的配置。

    Returns:
        被收编的条数。
    """
    if not owner_user_id:
        return 0
    try:
        rows = list(db.scalars(
            select(LlmProvider).where(LlmProvider.user_id.is_(None))
        ))
        if not rows:
            return 0

        existing = list(db.scalars(
            select(LlmProvider).where(LlmProvider.user_id == owner_user_id)
        ))
        taken = {(r.protocol, r.model, (r.base_url or "")) for r in existing}

        migrated = 0
        for row in rows:
            key = (row.protocol, row.model, (row.base_url or ""))
            if key in taken:
                db.delete(row)          # 已经有等价的一条了，删掉重复的
                logger.warning(
                    "[providers] 系统级供应商 %s 与已有的私有供应商重复，已删除", row.id
                )
                continue
            row.user_id = owner_user_id
            # 收编后就它就是"我的"供应商了；如果这人名下一条默认都没有，顺手设为默认，
            # 否则界面会显示"请选择模型"，多一步无谓操作。
            if not any(r.is_default for r in existing):
                row.is_default = True
            taken.add(key)
            migrated += 1

        # 之前"每 owner 至多一个默认"在跨 owner 时可能被破（系统级那条也是 default）
        _keep_single_default(db, owner_user_id)
        db.commit()
        if migrated:
            logger.warning(
                "[providers] 已把 %d 条系统级供应商收编给 %s（改成私有，密钥不变）",
                migrated, owner_user_id,
            )
        return migrated
    except Exception as e:  # noqa: BLE001 收编失败不该挡住启动
        logger.warning("[providers] 系统级供应商收编失败: %s", e)
        db.rollback()
        return 0


def _keep_single_default(db: OrmSession, user_id: str) -> None:
    """保证某用户名下最多一条 `is_default`（保留最早创建的那条）。"""
    rows = list(db.scalars(
        select(LlmProvider)
        .where(LlmProvider.user_id == user_id, LlmProvider.is_default.is_(True))
        .order_by(LlmProvider.created_at.asc())
    ))
    for extra in rows[1:]:
        extra.is_default = False


# ============================================================================
# 连通性探测（"测试连接"按钮）
# ============================================================================


def _classify_error(exc: Exception) -> tuple[str, str]:
    """把供应商 SDK 的异常翻成"人话 + 下一步动作"。

    ## 为什么要同时看三样东西

    只匹配异常类名是不够的：真实的报错经手好几层（供应商 SDK → httpx → 包装异常），
    落到这里时经常已经是一个普通 `Exception`，**有意义的信息全在 message 里**
    （比如 `404 model not found` 或 `Connection refused`）。

    所以判据是三者取并集：
    1. `status_code`（SDK 自己带的 HTTP 状态，最准）；
    2. 异常类名（`AuthenticationError`、`APIConnectionError` …）；
    3. message 文本（兜底，覆盖被包装过的情况）。

    判定顺序也有讲究：鉴权 → 权限 → 404 → 限流 → 400 → 连接/超时 → TLS → 兜底。
    越具体的放越前面，否则"404 model not found"会被连接类关键词抢走。

    Returns:
        `(给用户看的中文说明, 脱敏后的原始信息)`
    """
    detail = scrub_secrets(f"{type(exc).__name__}: {exc}")
    status = getattr(exc, "status_code", None)
    if status is None:
        status = getattr(exc, "status", None)
    # 类名 + message 合成一个待匹配的串，避免每处都写两遍条件
    hay = f"{type(exc).__name__} {exc}".lower()

    def has(*keys: str) -> bool:
        return any(k in hay for k in keys)

    if status == 401 or has("authenticationerror", "unauthorized", "invalid api key",
                            "invalid_api_key", "incorrect api key", "no api key",
                            "api key is invalid"):
        return "API Key 无效或已过期，请检查后重新填写", detail
    if status == 403 or has("permissiondenied", "permission", "forbidden"):
        return "该 API Key 没有访问这个模型的权限", detail
    if status == 404 or has("notfound", "not found", "does not exist", "no such model",
                            "unknown model", "model_not_found"):
        return "base_url 或模型名不存在（404）。请确认地址是否要带 /v1，以及模型名拼写", detail
    if status == 429 or has("ratelimit", "rate limit", "too many requests", "quota"):
        return "触发限流（429）：密钥有效，但当前额度或频率受限", detail
    if status in (400, 422) or has("badrequest", "invalid request", "unprocessable"):
        if has("temperature"):
            return "该模型不支持 temperature 参数，请把它从配置里去掉", detail
        if has("max_tokens", "max output", "maxoutput"):
            return "该模型不接受当前的 max_tokens 设置，请调整或留空", detail
        return "请求被供应商拒绝（400/422），请核对模型名与参数", detail
    if has("connect", "connection", "timeout", "timed out", "unreachable", "refused",
           "getaddrinfo", "nodename", "network", "proxy"):
        return "连接不上该地址：请确认 base_url 可达（本地模型注意端口/是否已启动；远端注意网络与代理）", detail
    if has("ssl", "certificate"):
        return "TLS 证书校验失败：请确认地址协议（http/https）是否正确", detail
    return f"调用失败：{detail[:200]}", detail


async def probe_provider(cfg: ProviderConfig) -> dict:
    """用该配置发一条最小请求，验证连通性与鉴权。

    Returns:
        `{"ok": bool, "message": str, "reply": str, "detail": str}`

    为什么要单独做这件事：**"密钥填错了"和"模型名填错了"在界面上完全一样**
    （都是"对话没反应"）。花一次几 token 的请求把它区分开，能省掉大量排查时间。
    """
    from langchain_core.messages import HumanMessage

    try:
        llm = build_llm(cfg)
    except Exception as e:  # noqa: BLE001 配置本身就不合法
        # ⚠️ 这里必须脱敏：pydantic 的 ValidationError message 里会**原样回显输入值**，
        # 而我们的输入里就有 api_key —— 不处理就会从"测试连接"按钮把明文密钥送出去。
        return {
            "ok": False,
            "message": f"配置不合法：{scrub_secrets(f'{type(e).__name__}: {e}')}",
            "reply": "",
            "detail": "",
        }

    try:
        resp = await llm.ainvoke([HumanMessage(content="ping")])
        text = getattr(resp, "content", "") or ""
        if isinstance(text, list):
            text = "".join(str(p.get("text", "")) if isinstance(p, dict) else str(p) for p in text)
        return {
            "ok": True,
            "message": "连接成功，可以正常使用",
            "reply": str(text).strip()[:60],
            "detail": "",
        }
    except Exception as e:  # noqa: BLE001 探测失败就是失败，不要抛
        message, detail = _classify_error(e)
        logger.info("[providers] 探测 %s 失败：%s", cfg.describe(), message)
        return {"ok": False, "message": message, "reply": "", "detail": detail[:400]}


__all__ = [
    "PROTOCOL_ANTHROPIC",
    "PROTOCOL_OPENAI",
    "ProviderNotConfigured",
    "config_from_row",
    "effective_base_url",
    "migrate_system_providers",
    "get_owned_provider",
    "parse_parameters",
    "probe_provider",
    "resolve_for_agent",
    "resolve_provider",
]
