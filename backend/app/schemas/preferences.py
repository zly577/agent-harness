"""Validated personal context and tool budgets; zero disables a budget."""
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


class AgentPreferences(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    context_max_messages: int = Field(ge=10, le=1000)
    context_keep_messages: int = Field(ge=1, le=999)
    context_window_tokens: int = Field(default=0, ge=0, le=10000000)
    context_output_reserve: int = Field(default=4096, ge=256, le=1000000)
    context_trigger_percent: int = Field(default=85, ge=30, le=95)
    context_keep_tokens: int = Field(default=6000, ge=256, le=1000000)
    tool_call_run_limit: int = Field(ge=0, le=100000)
    tool_call_thread_limit: int = Field(ge=0, le=100000)
    tool_call_limit_tool: str = Field(max_length=128)

    @field_validator("tool_call_limit_tool")
    @classmethod
    def tool_name(cls, value):
        import re
        value = value.strip()
        if value and not re.fullmatch(r"[\w.:-]+", value, flags=re.ASCII):
            raise ValueError("工具名只能包含字母、数字、下划线、点、冒号和连字符")
        return value

    @model_validator(mode="after")
    def consistent_limits(self):
        if self.context_window_tokens and self.context_window_tokens < self.context_output_reserve + 2048:
            raise ValueError("上下文窗口须大于输出预留至少 2048 token")
        if self.context_keep_messages >= self.context_max_messages:
            raise ValueError("保留消息数必须小于压缩阈值")
        if self.tool_call_thread_limit and self.tool_call_run_limit > self.tool_call_thread_limit:
            raise ValueError("每轮调用上限不能大于会话调用上限")
        return self


class MemoryUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    content: str = Field(max_length=100000)
    revision: str = Field(min_length=1, max_length=64)
