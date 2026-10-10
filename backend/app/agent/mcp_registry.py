"""MCP 配置来源与「数据库行 ↔ McpServerConfig」转换。

## 两个来源，一套语义

| 来源 | 谁在用 | 生效范围 | 改完要不要重启 |
|---|---|---|---|
| `backend/configs/mcp/*.yaml` | 运维预置 | **所有人** | 要（或调 `POST /api/v1/mcp/reload`） |
| 数据库 `mcp_servers` | 用户在界面/接口里新增 | **只有本人** | 不用（reload 会失效 agent 缓存） |

两者合并时，**同名以数据库为准**（跳过 YAML 那条）：YAML 是共享的，
数据库是用户自己的，让后者被前者盖掉会变成"我明明配了，怎么没生效"。

## 密钥边界

`headers` / `env` 整段加密后落库（Fernet）。列表接口想显示"配了哪些 HTTP 头"，
读 `header_keys` 这一列（**只有键名**）即可 —— 不要为了展示去解密。
"""
from __future__ import annotations

import json
import logging

from sqlalchemy import select
from sqlalchemy.orm import Session as OrmSession

from app.agent.mcp import McpServerConfig, load_all_mcp_cfg
from app.core.crypto import decrypt_secret, encrypt_secret
from app.db.models import McpServer
from app.db.session import SessionLocal

logger = logging.getLogger(__name__)


# ============================================================================
# 行 → 配置
# ============================================================================


def _loads_dict(raw: str) -> dict[str, str]:
    """密文解出来的 JSON 必须是 dict[str,str]。

    MCP 的 headers/env 只接受字符串值；如果库里存了别的形状（旧数据/手改），
    在这里拦住比让它在 httpx 深处炸掉好查得多。
    """
    if not raw:
        return {}
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        logger.warning("[mcp] 密文解出来不是合法 JSON，已按空处理")
        return {}
    if not isinstance(data, dict):
        return {}
    return {str(k): str(v) for k, v in data.items() if v is not None}


def _loads_list(raw: str) -> list[str]:
    if not raw:
        return []
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        return []
    return [str(x) for x in data] if isinstance(data, list) else []


def config_from_row(row: McpServer) -> McpServerConfig:
    """数据库行 → McpServerConfig（解密 headers/env）。

    Raises:
        ValueError: 密文与当前 FERNET_KEY 不匹配（crypto 已给出可操作的中文）。
            调用方应把它翻成"请重新填写密钥"，不要把底层异常直接抛给用户 ——
            那会连密文一起打出去。
    """
    headers = _loads_dict(decrypt_secret(row.headers_encrypted)) if row.headers_encrypted else {}
    env = _loads_dict(decrypt_secret(row.env_encrypted)) if row.env_encrypted else {}
    return McpServerConfig(
        name=row.name or "",
        transport=row.transport or "streamable_http",
        url=row.url or "",
        headers=headers,
        command=row.command or "",
        args=_loads_list(row.args_json),
        env=env,
        cwd=row.cwd or "",
        enabled=bool(row.enabled),
    )


def args_of(row: McpServer) -> list[str]:
    """stdio 的命令参数。"""
    return _loads_list(row.args_json)


def header_keys(row: McpServer) -> list[str]:
    """已配置的 HTTP 头**键名**（不解密，列表展示用）。"""
    return _loads_list(row.header_keys)


def has_env(row: McpServer) -> bool:
    return bool(row.env_encrypted)


def has_headers(row: McpServer) -> bool:
    return bool(row.headers_encrypted)


# ============================================================================
# 聚合：给 runtime 用
# ============================================================================


def load_mcp_configs(user_id: str | None = None) -> list[McpServerConfig]:
    """合并「该用户的数据库配置」+「YAML 预置」，返回要加载的全部配置。

    已在这里按 `enabled` 过滤（调用方不需要再判一遍 —— 两处各自过滤迟早不一致）。

    Args:
        user_id: 为空时只返回 YAML 那份（老行为，测试与探针用）。
    """
    db_cfg: list[McpServerConfig] = []
    if user_id:
        db = SessionLocal()
        try:
            for row in db.scalars(
                select(McpServer)
                .where(McpServer.user_id == user_id, McpServer.enabled.is_(True))
                .order_by(McpServer.created_at.asc())
            ):
                try:
                    db_cfg.append(config_from_row(row))
                except ValueError as e:
                    # 密钥解不开：跳过这一条，但必须留痕 ——
                    # 否则用户的感受是"配了没生效"，而日志里什么都没有。
                    logger.warning("[mcp] 跳过 %s：%s", row.name, e)
        finally:
            db.close()

    try:
        yaml_cfg = [c for c in load_all_mcp_cfg() if c.enabled]
    except Exception as e:  # noqa: BLE001 YAML 写坏了不能让整个 agent 起不来
        logger.warning("[mcp] configs/mcp/*.yaml 解析失败，已忽略: %s: %s", type(e).__name__, e)
        yaml_cfg = []

    taken = {c.name for c in db_cfg}
    merged = list(db_cfg)
    for c in yaml_cfg:
        if c.name in taken:
            logger.warning("[mcp] YAML 里的 '%s' 与数据库配置同名，已跳过 YAML 那条", c.name)
            continue
        merged.append(c)
    return merged


# ============================================================================
# 仓储（给 API 用）
# ============================================================================


def list_rows(db: OrmSession, user_id: str) -> list[McpServer]:
    """该用户的全部 MCP server（含停用的 —— 列表要能看到才能改回来）。"""
    return list(db.scalars(
        select(McpServer)
        .where(McpServer.user_id == user_id)
        .order_by(McpServer.created_at.asc())
    ))


def owned_row(db: OrmSession, server_id: str, user_id: str) -> McpServer | None:
    """按 id 取**属于自己的**行；不存在或是别人的都返回 None（→ 404）。

    刻意不做 `is_admin` 例外：MCP 没有"系统级共享"这一层（见 models 注释），
    管理员也不应该越过归属去改别人的工具源。
    """
    row = db.get(McpServer, server_id)
    if row is None or row.user_id != user_id:
        return None
    return row


def apply_config(row: McpServer, cfg: McpServerConfig) -> None:
    """把一份配置写进行（**含加密**）。headers/env 空字典 = 清空。"""
    row.name = cfg.name
    row.transport = cfg.transport
    row.url = cfg.url or ""
    row.headers_encrypted = encrypt_secret(json.dumps(cfg.headers, ensure_ascii=False)) if cfg.headers else ""
    row.header_keys = json.dumps(sorted(cfg.headers.keys()), ensure_ascii=False) if cfg.headers else ""
    row.command = cfg.command or ""
    row.args_json = json.dumps(cfg.args, ensure_ascii=False) if cfg.args else ""
    row.env_encrypted = encrypt_secret(json.dumps(cfg.env, ensure_ascii=False)) if cfg.env else ""
    row.cwd = cfg.cwd or ""
    row.enabled = bool(cfg.enabled)


__all__ = [
    "apply_config",
    "args_of",
    "config_from_row",
    "has_env",
    "has_headers",
    "header_keys",
    "list_rows",
    "load_mcp_configs",
    "owned_row",
]
