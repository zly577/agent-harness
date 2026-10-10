"""开发环境的密钥兜底。

## 为什么需要这个模块

`.env` 里 `JWT_SECRET` / `FERNET_KEY` 留空时，如果就地 `secrets.token_hex()` 生成一个，
会有两个都很糟的后果：

- **JWT_SECRET 每进程一变** ⇒ 后端一重启（`--reload` 就更频繁）所有人被登出；
- **FERNET_KEY 每进程一变** ⇒ 库里已加密的供应商 api_key **永久解不开**，
  而且报错发生在"用户点测试连接"的时候，现场完全看不出跟密钥有关。

所以留空时不是"随机生成一个用完就算"，而是**落盘到一个文件，之后长期复用**：

    backend/data/.dev_secrets.json      # 形如 {"jwt_secret": "...", "fernet_key": "..."}

`backend/data/` 已在 `.gitignore` 里（与 SQLite 消息历史同目录），所以不会入库。

## 边界

- **只在开发环境生效**（`settings.is_development`）。非开发环境缺密钥时，
  `Settings._check_production_secrets` 会直接让应用启动失败 —— 宁可开不起来，
  也不能让"悄悄生成的密钥"上线。
- 文件读写失败（只读盘 / 权限）不抛异常，退化为"本次进程内随机值 + WARNING"，
  保证开发能继续，但会明确告诉用户后果。
"""
from __future__ import annotations

import json
import logging
import secrets
from pathlib import Path
from typing import Callable

logger = logging.getLogger(__name__)

#: 落盘位置：与 SQLite 消息历史同一个目录（已被 .gitignore 覆盖）
_SECRETS_FILE = Path(__file__).resolve().parent.parent.parent / "data" / ".dev_secrets.json"

#: 进程内缓存，避免每次构造 LLM 都读盘
_cache: dict[str, str] = {}


def _load() -> dict:
    """读整个文件；不存在/损坏都返回空 dict（损坏时会被下一次写入覆盖）。"""
    if not _SECRETS_FILE.exists():
        return {}
    try:
        data = json.loads(_SECRETS_FILE.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except Exception as e:  # noqa: BLE001 文件坏了不该让应用起不来
        logger.warning("[secrets] %s 解析失败（将重建）: %s", _SECRETS_FILE.name, e)
        return {}


def _save(data: dict) -> None:
    try:
        _SECRETS_FILE.parent.mkdir(parents=True, exist_ok=True)
        _SECRETS_FILE.write_text(
            json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8"
        )
    except OSError as e:
        logger.warning("[secrets] 写入 %s 失败: %s", _SECRETS_FILE, e)


def resolve_secret(configured: str, name: str, factory: Callable[[], str]) -> str:
    """取一个密钥：优先用配置值；为空则走开发环境落盘兜底。

    Args:
        configured: `.env` 里配的值（可能为空）。
        name: 存进 `.dev_secrets.json` 的键名。
        factory: 生成函数（如 `lambda: secrets.token_urlsafe(48)`）。

    Returns:
        最终使用的密钥字符串。

    Raises:
        RuntimeError: 生产环境（非 development）且 `configured` 为空。
            —— 正常情况下 `Settings` 的校验会先拦住，这里是第二道保险。
    """
    value = (configured or "").strip()
    if value:
        return value

    from app.core.config import settings

    if not settings.is_development:
        raise RuntimeError(
            f"APP_ENV={settings.app_env} 下 {name.upper()} 必须显式配置，"
            "不允许自动生成（自动生成的密钥重启即变，会让已加密的数据永久解不开）。"
        )

    if name in _cache:
        return _cache[name]

    data = _load()
    if not (data.get(name) or "").strip():
        data[name] = factory()
        _save(data)
        logger.warning(
            "[secrets] %s 未配置，已在 %s 生成并长期复用（开发环境）。"
            "对外部署请写进 .env。",
            name.upper(),
            _SECRETS_FILE,
        )
    _cache[name] = data[name]
    return _cache[name]


def reset_cache() -> None:
    """清掉进程内缓存（测试用：切 .env 后需要重新读盘）。"""
    _cache.clear()


__all__ = ["resolve_secret", "reset_cache"]
