"""人工介入审批核心（P8）。

利用 deepagents 内置的 HITL 机制：
- 通过 `permissions=[FilesystemPermission(mode="interrupt", ...)]`，deepagents 自动把
  敏感路径操作（如写 /system/、/admin/）变成审批中断。
- 运行时中断后，graph 处于 pending 状态，可从 checkpoint 读取待审批的 action_requests。
- 审批通过/拒绝后，用 `Command(resume=HITLResponse)` 恢复 graph 继续执行。

本模块提供：
- read_pending_approval(agent, config)：读取一条会话当前待审批项。
- decide(agent, config, decision)：批准/拒绝并恢复。
- 供 FastAPI 审批路由调用（见 app/api/routes/approvals.py）。

说明：HITLResponse.decisions 与参与中断的 tool_call 一一对应；
approve → 继续执行；reject → 跳过该工具。
"""
from __future__ import annotations

import logging
from typing import Any

from langgraph.types import Command

logger = logging.getLogger(__name__)


async def read_pending_approval(agent, config: dict) -> dict | None:
    """读取该会话当前待审批的中断信息（如有）。

    返回形如:
        {"thread_id": ..., "pending": True, "actions": [ {name, args, description?} ]}
    无中断时返回 {"pending": False}。
    """
    state = await agent.aget_state(config)
    # 中断由 HITLMiddleware 在工具节点触发后，next 指向 tools；interrupt value 即 HITLRequest
    interrupts = getattr(state, "interrupts", None) or []
    if not interrupts:
        return {"pending": False}

    # 取第一个中断的 value（HITLRequest: {"action_requests": [...], ...}）
    inter = interrupts[0]
    value = inter.value if hasattr(inter, "value") else inter
    actions = []
    if isinstance(value, dict):
        for ar in value.get("action_requests", []):
            if isinstance(ar, dict):
                actions.append({
                    "type": ar.get("type", "tool_call"),
                    "tool": ar.get("tool") or (ar.get("tool_call") or {}).get("name", ""),
                    "args": ar.get("args") or (ar.get("tool_call") or {}).get("args", {}),
                })
    return {
        "thread_id": config.get("configurable", {}).get("thread_id"),
        "pending": True,
        "actions": actions,
    }


async def decide(agent, config: dict, decision: list[dict]) -> Any:
    """对挂起的中断做决策并恢复。

    decision: 与待审批 action 一一对应的决策列表，如 [{"type": "approve"}]
             或 [{"type": "reject"}]。
    恢复后 graph 继续执行，返回最终结果。
    """
    resume_value = {"decisions": decision}
    result = await agent.ainvoke(Command(resume=resume_value), config)
    return result


def build_decision(actions: list[dict] | None, approve: bool) -> list[dict]:
    """为所有待审批 action 构造 approve/reject 决策列表。"""
    count = len(actions) if actions else 1
    return [{"type": "approve" if approve else "reject"} for _ in range(count)]
