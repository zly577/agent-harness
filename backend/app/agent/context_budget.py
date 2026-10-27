"""Model-aware input budgets; estimates are conservative, never exact usage."""
from dataclasses import dataclass

from langchain_core.messages.utils import count_tokens_approximately

from app.core.config import settings
from langchain_core.messages import SystemMessage


class TodoPromptProjection:
    """Include TodoListMiddleware's inner prompt fragment in input estimates."""
    name = "TodoPromptProjection"

    def __init__(self, middleware):
        self.middleware = middleware
        self.tools = middleware.tools

    def modify_request(self, request):
        content = list(request.system_message.content_blocks) if request.system_message else []
        content.append({"type": "text", "text": "\n\n" + self.middleware.system_prompt})
        return request.override(system_message=SystemMessage(content=content))


@dataclass(frozen=True)
class ContextBudget:
    window: int
    input_limit: int
    trigger: int
    keep: int
    source: str


def estimate_tokens(messages, *, tools=None):
    # Two characters/token is deliberately conservative for mixed Chinese/code.
    # Provider-reported input usage calibrates subsequent requests upwards.
    return count_tokens_approximately(messages, tools=tools, chars_per_token=2.0)


def resolve_budget(model, preferences=None, model_settings=None):
    values = preferences or {}
    profile = getattr(model, "profile", None) or {}
    explicit = values.get("context_window_tokens", settings.context_window_tokens)
    known = profile.get("max_input_tokens")
    if explicit:
        window, source = explicit, "configured"
    elif isinstance(known, int) and known > 0:
        window, source = known, "model_profile"
    else:
        window, source = settings.context_fallback_tokens, "fallback"
    requested = (model_settings or {}).get("max_tokens") or (model_settings or {}).get("max_completion_tokens")
    model_output = getattr(model, "max_tokens", None) or getattr(model, "max_completion_tokens", None)
    reserve = max(values.get("context_output_reserve", settings.context_output_reserve),
                  requested if isinstance(requested, int) else 0,
                  model_output if isinstance(model_output, int) else 0)
    input_limit = window - reserve - settings.context_safety_tokens
    if input_limit < 1024:
        raise ValueError("上下文窗口不足：请增大窗口配置或减少输出预留 token")
    trigger = input_limit * values.get("context_trigger_percent", settings.context_trigger_percent) // 100
    keep = min(values.get("context_keep_tokens", settings.context_keep_tokens), trigger // 2)
    return ContextBudget(window, input_limit, trigger, max(1, keep), source)
