"""MCP 接口的请求/响应模型。

## 一条硬规则：headers / env 的值永不出现在响应里

- 列表/详情只回 `header_keys`（**只有键名**）与两个布尔 `has_headers` / `has_env`；
- 更新时 `headers` / `env` 传 `null` = 不动已存的值，传 `{}`（空字典）= 清空。
  这两个语义必须分开，否则"只是改个名字"会把密钥顺手抹掉；
- 连通性测试的失败原因过一遍 `scrub_secrets()` —— MCP SDK 报错时可能把
  `Authorization: Bearer xxx` 带进 message，而这条会直接显示给用户。

## url 的校验

只做形状校验（http/https + 有主机名），**不做连通性校验**：
内网地址、需要代理的地址、还没启动的本地 server 都合法，在这里 reject 掉
只会挡住正常用法。连通性由"测试连接"负责。
"""
from __future__ import annotations

from datetime import datetime
from urllib.parse import urlparse

from pydantic import BaseModel, Field, field_validator

from app.agent.mcp import SUPPORTED_TYPES, McpServerConfig

DEFAULT_TRANSPORT = "streamable_http"


def _validate_url(value: str | None) -> str:
    text = (value or "").strip()
    if not text:
        return ""
    parsed = urlparse(text)
    if parsed.scheme not in ("http", "https", "ws", "wss"):
        raise ValueError("url 必须以 http:// 或 https:// 开头（websocket 端点可用 ws:// / wss://）")
    if not parsed.hostname:
        raise ValueError("url 缺少主机名（例如 https://mcp.example.com/mcp）")
    if parsed.username or parsed.password:
        raise ValueError("url 不要带用户名/口令（请改用 headers 里的 Authorization）")
    return text


def _validate_transport(value: str | None) -> str:
    text = (value or DEFAULT_TRANSPORT).strip().lower()
    if text not in SUPPORTED_TYPES:
        raise ValueError(f"transport 只能是 {'/'.join(SUPPORTED_TYPES)}")
    return text


class McpServerOut(BaseModel):
    """对外暴露的 MCP server（**不含 headers/env 的值**）。"""

    id: str
    name: str
    transport: str
    url: str = ""
    command: str = ""
    args: list[str] = Field(default_factory=list)
    cwd: str = ""
    enabled: bool = True
    #: 只回键名（如 ["Authorization"]），值永远不回
    header_keys: list[str] = Field(default_factory=list)
    has_headers: bool = False
    has_env: bool = False
    created_at: datetime | None = None
    updated_at: datetime | None = None


class McpServerCreate(BaseModel):
    """新增一个 MCP 工具源。"""

    name: str = Field(..., min_length=1, max_length=128, description="唯一名，也用作工具前缀来源")
    transport: str = Field(DEFAULT_TRANSPORT, description="streamable_http / sse / stdio / websocket")
    url: str = Field("", description="远程端点（streamable_http / sse / websocket 必填）")
    headers: dict[str, str] = Field(default_factory=dict, description="HTTP 头，如 Authorization")
    command: str = Field("", description="stdio：启动命令")
    args: list[str] = Field(default_factory=list, description="stdio：命令参数")
    env: dict[str, str] = Field(default_factory=dict, description="stdio：子进程环境变量")
    cwd: str = Field("", description="stdio：子进程工作目录")
    enabled: bool = Field(True)

    @field_validator("url")
    @classmethod
    def _url(cls, v: str) -> str:
        return _validate_url(v)

    @field_validator("transport")
    @classmethod
    def _transport(cls, v: str) -> str:
        return _validate_transport(v)

    def to_config(self) -> McpServerConfig:
        """转成适配层配置（顺带做一遍"该 transport 必填项"的校验）。

        这里**故意**再构造一次 McpServerConfig：pydantic 层的校验只管形状，
        "stdio 必须有 command" 这类跨字段规则在 McpServerConfig 里，
        让它抛出来的 ValueError 由路由层翻成 400 的可读中文。
        """
        return McpServerConfig(
            name=self.name.strip(),
            transport=self.transport,
            url=self.url,
            headers=self.headers,
            command=self.command,
            args=self.args,
            env=self.env,
            cwd=self.cwd,
            enabled=self.enabled,
        )


class McpServerUpdate(BaseModel):
    """修改 MCP 工具源。**所有字段可选**，不传 = 不改。

    `headers` / `env` 传 `null` = 不动；传 `{}` = 清空。
    """

    name: str | None = Field(None, min_length=1, max_length=128)
    transport: str | None = None
    url: str | None = None
    headers: dict[str, str] | None = Field(None, description="null=不改；{ }=清空")
    command: str | None = None
    args: list[str] | None = None
    env: dict[str, str] | None = Field(None, description="null=不改；{ }=清空")
    cwd: str | None = None
    enabled: bool | None = None

    @field_validator("url")
    @classmethod
    def _url(cls, v: str | None) -> str | None:
        return v if v is None else _validate_url(v)

    @field_validator("transport")
    @classmethod
    def _transport(cls, v: str | None) -> str | None:
        return None if v is None else _validate_transport(v)

    def merged_config(self, current: McpServerConfig) -> McpServerConfig:
        """把本次改动并到现有配置上（未提供的字段沿用原值）。"""
        def pick(new, old):
            return old if new is None else new

        return McpServerConfig(
            name=(self.name or current.name).strip(),
            transport=pick(self.transport, current.transport),
            url=pick(self.url, current.url),
            headers=pick(self.headers, current.headers),
            command=pick(self.command, current.command),
            args=pick(self.args, current.args),
            env=pick(self.env, current.env),
            cwd=pick(self.cwd, current.cwd),
            enabled=pick(self.enabled, current.enabled),
        )


class McpTestRequest(McpServerCreate):
    """临时配置的连通性测试（"先测后存"，不落库）。"""

    timeout: float | None = Field(None, description="本次测试的超时（秒），不传用系统默认")


class McpTestResult(BaseModel):
    """连通性测试结果。"""

    ok: bool
    #: 给用户的中文结论
    message: str
    #: 真的拿到了哪些工具（**这是"能用"的证据**，不只是"TCP 通了"）
    tools: list[str] = Field(default_factory=list)
    #: 脱敏后的原始错误（失败时，便于排查但不泄漏密钥）
    detail: str = ""


class McpStatusOut(BaseModel):
    """MCP 总开关与依赖状态（前端据此提示"总开关没开"）。"""

    enabled: bool
    adapters_installed: bool
    server_count: int
    supported_transports: list[str] = Field(default_factory=lambda: list(SUPPORTED_TYPES))
    connect_timeout_seconds: float


__all__ = [
    "DEFAULT_TRANSPORT",
    "McpServerCreate",
    "McpServerOut",
    "McpServerUpdate",
    "McpStatusOut",
    "McpTestRequest",
    "McpTestResult",
]
