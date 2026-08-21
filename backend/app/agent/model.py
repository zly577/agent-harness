"""LLM 工厂：根据全局配置构建模型实例（P1 最小版本：单一 DeepSeek 模型）。"""
from langchain_deepseek import ChatDeepSeek

from app.core.config import settings


def build_llm() -> ChatDeepSeek:
    """构建 DeepSeek 模型实例。

    后续阶段（多模型/多 provider）在此扩展为按配置选择，统一返回 BaseChatModel。
    """
    return ChatDeepSeek(
        model=settings.deepseek_model,
        api_key=settings.deepseek_api_key,
        base_url=settings.deepseek_base_url,
        temperature=0,
        timeout=60,
    )
