"""供应商 api_key 的对称加密与脱敏。

## 用 Fernet（AES-128-CBC + HMAC-SHA256）

`cryptography.fernet.Fernet` 是"能直接用"的对称加密：一个密钥、密文自带
时间戳与 HMAC，破解成本远高于供应商密钥本身的价值，且不给我们留"自己拼 IV"的机会
（自己实现才是风险来源）。

## 明文密钥的可见边界（硬性）

`decrypt_secret()` 的结果**只允许出现在真正发起 LLM 请求的那一行**。具体禁止：

- 不进日志（`logger.*` 的格式化参数里不许带 key 或完整 base_url+key）
- 不进 `audit_logs.detail`
- 不进 SSE 事件
- 不进异常信息与堆栈 —— 供应商 SDK 报错时可能把 `Authorization` 头带进 message，
  所以对外返回前统一过一遍 `scrub_secrets()`。

## 密钥来源

`settings.fernet_key`；为空时开发环境落盘兜底（`backend/data/.dev_secrets.json`）。
⚠️ FERNET_KEY 一旦丢失或更换，**已存的密文全部解不开**，所以生产必须备份。
"""
from __future__ import annotations

import logging
import re
import secrets

from cryptography.fernet import Fernet, InvalidToken

from app.core.config import settings
from app.core.secrets_store import resolve_secret

logger = logging.getLogger(__name__)

_cipher: Fernet | None = None

#: 密钥形状：OpenAI 系 `sk-...`、Anthropic `sk-ant-...`。长前缀写在前面。
_SK_PATTERNS = (
    re.compile(r"sk-ant-[A-Za-z0-9_\-]{8,}"),
    re.compile(r"sk-[A-Za-z0-9_\-]{8,}"),
)

#: `key=value` / `key: value` 形态 —— 只看键名，值一律擦掉。
_KV_PATTERN = re.compile(
    r"(?i)\b(api[_-]?key|apikey|authorization|access[_-]?token|token)\b(\s*[:=]\s*)(\S+)"
)

_REDACTED = "****"


def _redact_kv(match: re.Match) -> str:
    """保留 `api_key=` 这样的键名（有助排查），只把值换成 ****。"""
    return f"{match.group(1)}{match.group(2)}{_REDACTED}"


def scrub_secrets(text: str) -> str:
    """把文本里疑似密钥的片段擦掉（用于异常信息、错误原因等**要回给前端**的内容）。

    这不是"加密"，只是一道防泄漏的兜底：供应商 SDK / httpx 在报错时可能把
    请求头或 URL 带进 message（比如 `401 for url: https://.../v1/chat?key=sk-xxx`）。
    """
    if not text:
        return ""
    out = str(text)
    for pattern in _SK_PATTERNS:
        out = pattern.sub(_REDACTED, out)
    return _KV_PATTERN.sub(_redact_kv, out)



def _fernet() -> Fernet:
    """取 Fernet 实例（进程内复用）。密钥非法时给出可操作的报错。"""
    global _cipher
    if _cipher is None:
        key = resolve_secret(
            settings.fernet_key,
            "fernet_key",
            lambda: Fernet.generate_key().decode(),
        )
        try:
            _cipher = Fernet(key.encode("utf-8") if isinstance(key, str) else key)
        except Exception as e:  # noqa: BLE001 配置错误要说人话
            raise RuntimeError(
                "FERNET_KEY 非法，无法初始化加解密。生成方式："
                'python -c "from cryptography.fernet import Fernet; '
                'print(Fernet.generate_key().decode())"'
            ) from e
    return _cipher


def reset_cipher() -> None:
    """丢掉缓存的 Fernet（测试用：切密钥后需要重建）。"""
    global _cipher
    _cipher = None


def encrypt_secret(plaintext: str) -> str:
    """明文 → Fernet 密文（str，便于直接存 Text 列）。"""
    return _fernet().encrypt((plaintext or "").encode("utf-8")).decode("ascii")


def decrypt_secret(ciphertext: str) -> str:
    """密文 → 明文。

    Raises:
        ValueError: 密文损坏或与当前 FERNET_KEY 不匹配。
            调用方应把它翻成"该供应商的密钥无法解密，请重新填写"，
            而不是把底层异常直接抛给用户（那会连密文一起打出来）。
    """
    try:
        return _fernet().decrypt((ciphertext or "").encode("ascii")).decode("utf-8")
    except InvalidToken as e:
        raise ValueError(
            "密钥无法解密（可能是 FERNET_KEY 被更换，或该记录被其它密钥写过）。"
            "请重新填写该供应商的 API Key。"
        ) from e


def mask_secret(plaintext: str, *, head: int = 3, tail: int = 4) -> str:
    """脱敏展示：`sk-1234567890abcd` → `sk-****abcd`。

    接口一律只回掩码（`llm_providers.api_key_hint` 是**落库时**就算好存下的，
    列表接口根本不需要解密），所以明文密钥不会因为"看一眼列表"而进入进程内存。

    太短的串直接全遮（否则遮了等于没遮）。
    """
    text = plaintext or ""
    if not text:
        return ""
    if len(text) <= head + tail:
        return "*" * len(text)
    return f"{text[:head]}****{text[-tail:]}"


def random_api_key_placeholder() -> str:
    """测试/演示用（别用在真实配置里）。"""
    return f"sk-{secrets.token_hex(16)}"


__all__ = [
    "decrypt_secret",
    "encrypt_secret",
    "mask_secret",
    "random_api_key_placeholder",
    "reset_cipher",
    "scrub_secrets",
]
