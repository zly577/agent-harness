"""模型供应商接口的请求/响应模型。

## 一条硬规则：明文密钥永不出现在响应里

- 列表/详情一律只回 `api_key_hint`（落库时就算好的掩码，如 `sk-****1234`）；
- 更新时 `api_key` **留空 = 不改动已存的密钥**（这样前端不必为了改个名字而回填密钥，
  也就不会把明文拉回浏览器）；
- 测试连接（临时配置）的响应里只有"成功/失败 + 原因"，回显的 `detail` 也过了一遍
  `crypto.scrub_secrets()` —— 供应商 SDK 报错时可能把请求头带进 message。

## base_url 的校验

只做"基本校验"（§4.4）：必须是 http/https、必须能解析出主机名。
刻意**不**做连通性校验 —— 内网地址、需要代理的地址、还没启动的本地模型都合法，
在这里 reject 掉只会挡住正常用法。真正的连通性由"测试连接"负责。
"""
from __future__ import annotations

from datetime import datetime
from urllib.parse import urlparse

from pydantic import BaseModel, Field, field_validator

from app.agent.model import PROTOCOL_ANTHROPIC, PROTOCOL_OPENAI, PROTOCOLS

#: 参数 JSON 允许的键（其余原样透传给供应商 SDK，但给出提示便于排错）
KNOWN_PARAMETER_KEYS = ("temperature", "timeout", "max_tokens", "top_p", "frequency_penalty",
                        "presence_penalty", "stop", "seed", "supports_vision")


def _validate_base_url(value: str | None) -> str | None:
    """校验 base_url 的基本形状。空串归一为 None（= 用官方默认地址）。"""
    text = (value or "").strip()
    if not text:
        return None
    parsed = urlparse(text)
    if parsed.scheme not in ("http", "https"):
        raise ValueError("base_url 必须以 http:// 或 https:// 开头")
    if not parsed.hostname:
        raise ValueError("base_url 缺少主机名（例如 https://api.openai.com/v1）")
    if parsed.username or parsed.password:
        # 把用户名密码写进 URL 会被各处日志顺手记下来，明确拒绝
        raise ValueError("base_url 不要带用户名/密码（请用 api_key 字段）")
    return text.rstrip("/")


class ProviderOut(BaseModel):
    """对外暴露的供应商信息（**不含明文密钥**）。"""

    id: str
    name: str
    protocol: str
    #: 实际使用的地址（留空时已填上该协议的官方默认值）
    base_url: str
    model: str
    parameters: dict = Field(default_factory=dict)
    is_default: bool = False
    enabled: bool = True
    #: True = 系统级共享（所有用户可用，仅 admin 可改）
    is_system: bool = False
    #: 掩码（列表展示用；不会返回明文）
    api_key_hint: str = ""
    #: 是否已配置密钥（前端据此提示"未填密钥"）
    has_api_key: bool = False
    created_at: datetime | None = None
    updated_at: datetime | None = None


class ProviderCreate(BaseModel):
    """新增供应商。默认建**自己私有**的；`scope="system"` 仅 admin 可用。"""

    name: str = Field("", max_length=128, description="展示名，如「我的 DeepSeek」")
    protocol: str = Field(PROTOCOL_OPENAI, description="openai / anthropic")
    base_url: str | None = Field(None, description="留空用官方默认地址")
    api_key: str = Field("", description="密钥（落库前加密；响应只回掩码）")
    model: str = Field(..., min_length=1, max_length=128, description="模型名，如 deepseek-chat")
    parameters: dict | None = Field(None, description="temperature / timeout / max_tokens 等")
    is_default: bool = Field(False, description="设为我的默认供应商；不填时第一条会自动成为默认")
    enabled: bool = Field(True)
    # 注：早期版本有 `scope="system"`（创建系统级共享供应商）。2026-09-18 起取消 ——
    # 改为"每个用户必须自己配模型"，系统级那一层退出解析，见 providers 模块头注释。

    @field_validator("base_url")
    @classmethod
    def _url(cls, v: str | None) -> str | None:
        return _validate_base_url(v)

    @field_validator("protocol")
    @classmethod
    def _protocol(cls, v: str) -> str:
        p = (v or "").strip().lower()
        if p not in PROTOCOLS:
            raise ValueError(f"protocol 只能是 {'/'.join(PROTOCOLS)}")
        return p


class ProviderUpdate(BaseModel):
    """修改供应商。**所有字段可选**；不传 = 不改。

    `api_key` 留空 = 保留原密钥（见模块头注释）。
    """

    name: str | None = Field(None, max_length=128)
    protocol: str | None = None
    base_url: str | None = None
    api_key: str | None = Field(None, description="留空 = 不改动已保存的密钥")
    model: str | None = Field(None, max_length=128)
    parameters: dict | None = None
    enabled: bool | None = None

    @field_validator("base_url")
    @classmethod
    def _url(cls, v: str | None) -> str | None:
        return _validate_base_url(v)

    @field_validator("protocol")
    @classmethod
    def _protocol(cls, v: str | None) -> str | None:
        if v is None:
            return None
        p = v.strip().lower()
        if p not in PROTOCOLS:
            raise ValueError(f"protocol 只能是 {'/'.join(PROTOCOLS)}")
        return p


class ProviderTestRequest(BaseModel):
    """临时配置的连通性测试（编辑时"先测后存"，不落库）。"""

    protocol: str = Field(PROTOCOL_OPENAI)
    base_url: str | None = None
    api_key: str = Field("", description="明文密钥，仅用于本次测试，不会入库")
    model: str = Field(..., min_length=1)
    parameters: dict | None = None

    @field_validator("base_url")
    @classmethod
    def _url(cls, v: str | None) -> str | None:
        return _validate_base_url(v)

    @field_validator("protocol")
    @classmethod
    def _protocol(cls, v: str) -> str:
        p = (v or "").strip().lower()
        if p not in PROTOCOLS:
            raise ValueError(f"protocol 只能是 {'/'.join(PROTOCOLS)}")
        return p


class ProviderTestResult(BaseModel):
    """测试连接的结果。"""

    ok: bool
    #: 给用户看的中文结论（例如"API Key 无效或已过期"）
    message: str
    #: 模型的实际回复片段（成功时；用于证明"真的连通了"而不只是 TCP 通了）
    reply: str = ""
    #: 脱敏后的原始错误（失败时，便于排查但不会泄漏密钥）
    detail: str = ""


class ModelListResponse(BaseModel):
    """供应商支持的模型列表。"""

    ok: bool
    models: list[str] = Field(default_factory=list)
    message: str = ""


__all__ = [
    "KNOWN_PARAMETER_KEYS",
    "ModelListResponse",
    "ProviderCreate",
    "ProviderOut",
    "ProviderTestRequest",
    "ProviderTestResult",
    "ProviderUpdate",
]
