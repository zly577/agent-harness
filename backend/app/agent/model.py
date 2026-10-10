"""LLM 工厂：按供应商配置构建模型实例（P18：OpenAI / Anthropic 双协议）。

## 这个模块负责什么

把「一条供应商配置」翻译成一个 `BaseChatModel`。三条协议路径：

| protocol | 类 | 适用 |
|----------|----|------|
| `openai` | `ChatOpenAI` | OpenAI 官方，以及所有兼容 Chat Completions 的：DeepSeek、Moonshot/Kimi、通义（兼容模式）、智谱（兼容模式）、OpenRouter、硅基流动、vLLM、Ollama、LM Studio |
| `openai` + DeepSeek 官方域名 | **`ChatDeepSeek`** | 见下面「为什么单独留一个 DeepSeek 分支」 |
| `anthropic` | `ChatAnthropic` | Anthropic Messages 协议（Claude 官方及兼容实现） |

## ⚠️ 三家的参数名不一样（实测 langchain-openai 1.3.2 / langchain-deepseek 1.1.0 / langchain-anthropic 1.5.3）

| | 模型名 | 密钥 | 地址 | 超时 |
|---|---|---|---|---|
| ChatOpenAI | `model` | `openai_api_key`（别名 `api_key`） | `openai_api_base`（别名 `base_url`） | `request_timeout`（别名 `timeout`） |
| ChatDeepSeek | `model` | `api_key` | `api_base`（别名 `base_url`） | `request_timeout`（别名 `timeout`） |
| **ChatAnthropic** | `model` | **`anthropic_api_key`** | **`anthropic_api_url`** | **`default_request_timeout`** |

Anthropic 那一列是**最容易踩的坑**：它**没有** `api_key` / `base_url` / `timeout` 这些名字，
而这三个类的 `model_config.extra` 都是 `ignore` —— 写错了**不会报错**，只会静默地
"用默认地址 + 空密钥"去请求，然后回一个看不懂的 401。所以这里一律用**各家自己的规范名**，
并在 `tests/test_p18_model_factory.py` 里逐个断言"构造出来的实例属性真的是我传的值"。

另外一个坑：**`max_tokens` 对 Anthropic 是必填**（Messages API 要求），
不给会在第一次调用时 400，所以工厂必须兜一个默认值。

## 为什么单独留一个 DeepSeek 分支

`ChatDeepSeek` 会对 `additional_kwargs.reasoning_content` 做专门解析 —— 也就是
"思考增量"。前端能实时看到模型在思考，全靠这个字段（P14 专门解决过"长推理期间黑屏"：
实测 31.8s 的回答里前 26.9s 只有 reasoning 在动）。

换成裸 `ChatOpenAI` 有丢失该字段的风险，因此**按 base_url 命中 DeepSeek 官方域名时
仍然走 `ChatDeepSeek`**；其余一律 `ChatOpenAI`。`ProviderConfig.dialect` 可以手工覆盖这个判定。
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any

from langchain_core.language_models.chat_models import BaseChatModel

from app.core.config import settings

logger = logging.getLogger(__name__)

PROTOCOL_OPENAI = "openai"
PROTOCOL_ANTHROPIC = "anthropic"
PROTOCOLS = (PROTOCOL_OPENAI, PROTOCOL_ANTHROPIC)

#: 官方默认地址（base_url 留空时用；仅用于展示与测试连接）
DEFAULT_BASE_URLS = {
    PROTOCOL_OPENAI: "https://api.openai.com/v1",
    PROTOCOL_ANTHROPIC: "https://api.anthropic.com",
}

#: 未在配置里指定 parameters 时使用的兜底参数。
#: - temperature=0：与改造前一致（确定性优先，便于调试与回归）
#: - timeout=60：单次请求超时（秒）
DEFAULT_PARAMETERS: dict[str, Any] = {"temperature": 0.0, "timeout": 60}

#: Anthropic 的 `max_tokens` 是必填项，没有配置时用这个值。
DEFAULT_ANTHROPIC_MAX_TOKENS = 4096

#: **不接受** `temperature` 的模型（按模型名前缀匹配）。
#: 这些是推理模型：它们只接受默认温度，传了会直接 400。
#: 与其等报错再让用户猜，不如构造时主动剔除并留一条日志。
_UNSUPPORTED_TEMPERATURE_PREFIXES = (
    "deepseek-reasoner",
    "o1",
    "o3",
    "o4",
    "gpt-5",
)

#: DeepSeek 官方域名的判定（用于保留 ChatDeepSeek 的 reasoning 解析）
_DEEPSEEK_HOST_HINTS = ("deepseek.com",)


@dataclass
class ProviderConfig:
    """一条可用的供应商配置（**api_key 已是明文**，只应存在于内存中）。

    明文的生命周期：`providers.resolve_provider()` 解密 → 传进 `build_llm()` →
    构造出模型实例后就不再被引用。**不要**把它写进日志、审计、SSE 或异常信息。
    日志/报错一律用 `describe()`（不含密钥）。
    """

    protocol: str = PROTOCOL_OPENAI
    model: str = ""
    api_key: str = ""
    base_url: str | None = None
    parameters: dict[str, Any] = field(default_factory=dict)
    #: 供应商展示名（日志/错误信息里用它，而不是 api_key）
    name: str = ""
    #: llm_providers.id；来自 .env 引导配置时为 None
    provider_id: str | None = None
    #: 系统级共享供应商（user_id IS NULL）
    is_system: bool = False
    #: `auto` / `openai` / `deepseek`：覆盖"是否用 ChatDeepSeek"的自动判定
    dialect: str = "auto"

    def describe(self) -> str:
        """用于日志/报错的**安全**描述（绝不含 api_key）。"""
        where = self.base_url or DEFAULT_BASE_URLS.get(self.protocol, "")
        origin = "系统级" if self.is_system else "用户私有"
        return f"{self.name or '(未命名)'}[{self.protocol}/{self.model}, {origin}, {where}]"


def env_bootstrap_config() -> ProviderConfig:
    """`.env` 里那组 DeepSeek 配置对应的供应商（P18 起它是"系统级供应商的引导值"）。

    刻意保留这个入口：升级前的老部署只有 `.env`，没有 `llm_providers` 表。
    启动引导会用它写出一条系统级供应商；而 `build_llm()` 无参调用时也用它兜底，
    这样既有的测试/探针不受影响。
    """
    return ProviderConfig(
        protocol=PROTOCOL_OPENAI,
        model=settings.deepseek_model,
        api_key=settings.deepseek_api_key,
        base_url=settings.deepseek_base_url,
        parameters=dict(DEFAULT_PARAMETERS),
        name="DeepSeek（.env 引导配置）",
        provider_id=None,
        is_system=True,
        dialect="deepseek",
    )


def _looks_like_deepseek(cfg: ProviderConfig) -> bool:
    """是否应该用 `ChatDeepSeek`（保留 reasoning_content 解析）。"""
    if cfg.dialect == "deepseek":
        return True
    if cfg.dialect == "openai":
        return False
    return any(h in (cfg.base_url or "") for h in _DEEPSEEK_HOST_HINTS)


def effective_parameters(cfg: ProviderConfig) -> tuple[dict[str, Any], list[str]]:
    """归一化参数：默认值兜底 + 剔除该模型不支持的项。

    Returns:
        `(参数, 被剔除的项名列表)` —— 剔除项会被记日志，
        免得用户疑惑"我明明设了 temperature 怎么没生效"。
    """
    params: dict[str, Any] = dict(DEFAULT_PARAMETERS)
    params.update({k: v for k, v in (cfg.parameters or {}).items() if v is not None})

    dropped: list[str] = []
    model_lower = (cfg.model or "").lower()
    if any(model_lower.startswith(p) for p in _UNSUPPORTED_TEMPERATURE_PREFIXES):
        if "temperature" in params:
            params.pop("temperature")
            dropped.append("temperature")
    return params, dropped


def build_llm(cfg: ProviderConfig | None = None) -> BaseChatModel:
    """按配置构建模型实例。

    Args:
        cfg: 供应商配置。传 None 时用 `.env` 的 DeepSeek 引导配置
            （保持向后兼容：既有测试与探针都是无参调用的）。

    Returns:
        `BaseChatModel`（ChatOpenAI / ChatDeepSeek / ChatAnthropic 之一）。

    Raises:
        ValueError: 协议不认识、或模型名为空。
    """
    cfg = cfg or env_bootstrap_config()

    protocol = (cfg.protocol or PROTOCOL_OPENAI).strip().lower()
    if protocol not in PROTOCOLS:
        raise ValueError(
            f"不支持的协议 {cfg.protocol!r}，只支持 {'/'.join(PROTOCOLS)}"
        )
    model = (cfg.model or "").strip()
    if not model:
        raise ValueError(
            f"供应商 {cfg.name or cfg.provider_id or '(未命名)'} 没有配置模型名（model）"
        )

    params, dropped = effective_parameters(cfg)
    if dropped:
        logger.info(
            "[model] %s 不支持 %s，已自动剔除（该模型只接受默认值）",
            model, "/".join(dropped),
        )

    timeout = params.pop("timeout", None)
    temperature = params.pop("temperature", None)

    if protocol == PROTOCOL_ANTHROPIC:
        # ⚠️ 参数名与其它两家**不同**，见模块头注释的表格。
        # extra="ignore"，写错不会报错 —— 所以这里只用规范名。
        kwargs: dict[str, Any] = {"anthropic_api_key": cfg.api_key or ""}
        if cfg.base_url:
            kwargs["anthropic_api_url"] = cfg.base_url
        if timeout is not None:
            kwargs["default_request_timeout"] = float(timeout)
        if temperature is not None:
            kwargs["temperature"] = temperature
        # max_tokens 必填：没给就兜默认值，否则第一次调用必 400
        kwargs["max_tokens"] = int(params.pop("max_tokens", None) or DEFAULT_ANTHROPIC_MAX_TOKENS)
        kwargs.update(params)  # 其余自定义参数原样透传
        from langchain_anthropic import ChatAnthropic

        return ChatAnthropic(model=model, **kwargs)

    # ---- openai 协议（含 DeepSeek 特化）----
    deepseek = _looks_like_deepseek(cfg)
    kwargs = {}
    if cfg.base_url:
        kwargs["base_url"] = cfg.base_url
    if timeout is not None:
        kwargs["timeout"] = float(timeout)
    if temperature is not None:
        kwargs["temperature"] = temperature
    # 流式块间超时：`timeout`（request_timeout）只管连接+首字节，管不住流式过程。
    # 推理模型在 reasoning 阶段卡住时会永久 await 下一个 chunk —— 必须靠
    # `stream_chunk_timeout` 兜底（见 config.llm_stream_chunk_timeout_seconds 注释）。
    chunk_timeout = settings.llm_stream_chunk_timeout_seconds
    if chunk_timeout and chunk_timeout > 0:
        kwargs["stream_chunk_timeout"] = float(chunk_timeout)
    kwargs.update(params)

    if deepseek:
        # 保留 ChatDeepSeek：思考增量（reasoning_content）靠它解析
        from langchain_deepseek import ChatDeepSeek

        return ChatDeepSeek(model=model, api_key=cfg.api_key or "", **kwargs)

    from langchain_openai import ChatOpenAI

    return ChatOpenAI(model=model, api_key=cfg.api_key or "", **kwargs)


__all__ = [
    "DEFAULT_ANTHROPIC_MAX_TOKENS",
    "DEFAULT_BASE_URLS",
    "DEFAULT_PARAMETERS",
    "PROTOCOL_ANTHROPIC",
    "PROTOCOL_OPENAI",
    "PROTOCOLS",
    "ProviderConfig",
    "build_llm",
    "effective_parameters",
    "env_bootstrap_config",
]
