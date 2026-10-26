"""Agent events mapped to the stable JSON/SSE wire contract."""
from typing import Any


def to_sse_event(kind: str, data: Any) -> dict:
    """runtime 事件 (kind, data) → 前端事件字典（前后端契约就在这里）。

    The runner and HTTP transports share this mapping; routes only encode SSE.
    """
    if kind == "token":
        return {"type": "token", "content": data}
    if kind == "thinking":
        return {"type": "thinking", **data}
    if kind == "tool_call":
        return {"type": "tool_call", **data}
    if kind == "tool_result":
        return {"type": "tool_result", **data}
    # 工具参数**还在生成**时的节流心跳（大参数要几十秒到几分钟）。
    # 它存在理由和 `thinking` 一样：那段时间本来一个事件都没有，用户无法区分
    # "在写参数"和"挂了"。**不含 args 正文**，只有体积/耗时，所以可以直接推给前端。
    if kind == "tool_args":
        return {"type": "tool_args", **data}
    # 子智能体过程（把 task 委派的内部进展暴露给前端）
    if kind == "subagent_start":
        return {"type": "subagent_start", **data}
    if kind == "subagent_token":
        return {"type": "subagent_token", **data}
    if kind == "subagent_thinking":
        return {"type": "subagent_thinking", **data}
    if kind == "subagent_tool":
        return {"type": "subagent_tool", **data}
    if kind == "subagent_end":
        return {"type": "subagent_end", **data}
    if kind == "subagent_usage":
        return {"type": "subagent_usage", **data}
    if kind == "usage":
        return {"type": "usage", **data}
    if kind in {"approval", "clarification"}:
        return {
            "type": kind,
            "clarifications": data.get("clarifications") or [],
            "interrupts": data.get("interrupts") or [],
            "pending": bool(data.get("pending")),
            "actions": data.get("actions") or [],
            # 停在审批上时，模型可能已经说过一段话了：把它带出去让前端落进气泡，
            # 否则用户看到的只有"弹窗"+"空气泡"（观感：工具都跑完了却什么都没说）。
            "text": data.get("text") or "",
        }
    if kind == "result":
        return {"type": "result", "content": data}
    if kind == "error":
        return {"type": "error", "detail": data}
    return {"type": kind, "content": data}
