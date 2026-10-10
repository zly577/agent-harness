"""子智能体（task 委派）的可见性改造：流式转发内部过程 + 超时保护 + 摘要。

## 为什么需要这个模块（排查结论）

deepagents 0.7.1 的 `task` 工具内部是这样跑的（`middleware/subagents.py` 的 `atask`）：

    result = await subagent.ainvoke(subagent_state, subagent_config)

**`ainvoke` 不是 `astream`**，所以在它返回之前，父级 `astream` 一个事件都不会产出。
实测一次委派：父级事件流里 `langgraph_node` 只有 `model`/`tools`，子智能体的 200+
条内部事件**完全没有**进入父流 —— 前端只能看到一个"执行中"的委派卡片，然后长时间
没有任何变化。这就是「不确定是挂了还是单纯慢」的根因：不是慢，是**过程不可观测**。

## 怎么让它可见（实测有效的两条）

1. **`astream(..., subgraphs=True)`**：事件元组第一项是命名空间，子智能体的内部事件
   带 `tools:<task 的 tool_call_id>` 前缀。实测 290 条 → 1038 条事件，其中 229 条
   来自子智能体命名空间。这是拿到内部 token 的**唯一**可行路径
   （callback 路线实测 metadata 为空、拿不到 `ls_agent_type`，不可行）。
2. **`usage_metadata`**：每个模型步结束时随分片到达，含精确的
   `input_tokens` / `output_tokens` / `output_token_details.reasoning`，
   前后端据此显示真实的 token 用量与速率（不用字符数估算）。

## 超时

子智能体没有超时上限时，一旦卡住就是无限期。这里给 `task` 工具套一层
`asyncio.wait_for`，超时后抛出可读错误 —— 主智能体会把它当普通工具失败处理并
汇报给用户，而不是让整轮对话悬死。

**2026-09-17 修复**：这条超时此前**从未生效**。原实现按
`agent.nodes["tools"].bound._tools` 找工具，但实测（langgraph 1.x）该 bound 是
`ToolNode`，工具真实存放在 `_tools_by_name`（dict）里、**没有 `_tools` 属性**，
于是函数直接 `return False`（只在日志里留一句 warning）。后果是"900 秒超时"
只是个摆设，子智能体真卡住就是无限等待 —— 正是"不知道是挂了还是要等"的根源之一。
现在改为遍历所有可能的存放形状（见 `_iter_bound_tools`）。

## 第三种静默：推理阶段（2026-09-17）

DeepSeek 推理模型的"思考"增量走 `additional_kwargs.reasoning_content`，
**不经过 `message.content`**。实测：一次 31.8s 的回答中，前 **26.9s** 全是推理，
`content` 一个字都没产出 —— 前端只订阅 `content`，于是这 26.9s 完全是黑屏。
子智能体同理（实测 66.6s 的调研里有 13.9s 静默）。

所以这里把推理增量也做成事件（`reasoning_delta()` + `iter_subagent_events` 里的
`subagent_thinking`），让"思考中"变成看得见的数字而不是一个转圈。
"""
from __future__ import annotations

import asyncio
import logging
import re
from typing import Any, AsyncIterator

logger = logging.getLogger(__name__)

#: 子智能体命名空间形如 `tools:<tool_call_id>`，取其中的调用 id。
_SUBAGENT_NS_RE = re.compile(r"^tools:(?P<id>[0-9a-zA-Z_-]+)")

#: 摘要最大长度（前端只展示摘要，全文仍可通过展开查看）
SUMMARY_LIMIT = 120


def parse_subagent_namespace(namespace: Any) -> str | None:
    """把 `subgraphs=True` 的命名空间解析成发起它的 task 调用 id。

    命名空间实为元组（如 `("tools:<id>",)`）或字符串；解析不出子智能体返回 None。
    """
    if namespace is None:
        return None
    parts = namespace if isinstance(namespace, (tuple, list)) else (namespace,)
    for part in parts:
        m = _SUBAGENT_NS_RE.match(str(part))
        if m:
            return m.group("id")
    return None


def summarize_task(text: str, limit: int = SUMMARY_LIMIT) -> str:
    """把委派给子智能体的长 description 压成一行摘要（给用户看的）。

    取第一行「有内容」的文本（跳过 markdown 标记与纯标题行），去掉常见前缀，
    再按长度截断。**只影响展示**，完整 description 仍随 tool_call 事件下发。
    """
    raw = (text or "").strip()
    if not raw:
        return ""

    for line in raw.splitlines():
        s = line.strip()
        # 跳过空行、markdown 标记、纯分隔线
        if not s or s.startswith(("#", ">", "---", "===", "```", "|")):
            continue
        s = re.sub(r"^[-*+]\s+", "", s)          # 列表符
        s = re.sub(r"^\*\*(.+?)\*\*[:：]?\s*", r"\1：", s)  # **任务目标**：
        if len(s) < 2:
            continue
        return s if len(s) <= limit else s[: limit - 1] + "…"

    flat = " ".join(raw.split())
    return flat if len(flat) <= limit else flat[: limit - 1] + "…"


class SubagentTimeoutError(Exception):
    """子智能体超过 `SUBAGENT_TIMEOUT_SECONDS` 未结束。"""


class SubagentCallRegistry:
    """把子图命名空间 id 归一到 **task 调用 id**（前端按这一个 id 归位）。

    ## 为什么必须有这一层

    langgraph 给子图分配的命名空间是 `tools:<PregelTask 的 uuid>`，那个 uuid
    与 `task` 工具的 `tool_call_id`（形如 `call_00_xxx`）**毫无关系**。实测：

        subagent_start / subagent_end / tool_result  → id = call_00_7fJG0yxB3ZqZrdOP
        subagent_token / subagent_thinking / ...     → id = 0d812bb3-6f62-f3bb-e55d-…

    后果（浏览器实测）：卡片按 `call_00_…` 建出来，只在 `subagent_start` 时拿到
    摘要；正文、推理、工具、用量全落到 `0d812bb3…` 这个**另一个 id** 上，于是
    **被渲染成第二张卡片**。用户看到的第一张卡片整整 100 秒只有标题和摘要、
    正文空白 —— 「不知道是挂了还是要等」的直接原因。

    解决：子图首个事件到达时，把它认领到**当时尚未被认领的那个 task 调用 id** 上
    （task 调用按发出顺序排队，子图也按同样顺序开始流式，故 FIFO 成立）。
    认领后，所有 `subagent_*` 事件统一改写成该 id，前端只用一套 id 逻辑。

    认领不到时（例如没走 subagent_start 就直接来内容事件）退回原始命名空间 id ——
    宁可 id 难看，也不能把事件丢掉。
    """

    def __init__(self) -> None:
        #: 已发出、尚未被任何子图认领的 task 调用 id（FIFO）
        self._unclaimed: list[str] = []
        #: 命名空间 id → task 调用 id
        self._alias: dict[str, str] = {}
        #: 已结束的 task 调用 id（不再参与认领）
        self._finished: set[str] = set()

    def note_task_call(self, call_id: str) -> None:
        """记下一次 `task` 调用（用于后续认领）。"""
        cid = str(call_id or "")
        if cid and cid not in self._finished:
            if cid not in self._unclaimed:
                self._unclaimed.append(cid)

    def note_task_finished(self, call_id: str) -> None:
        """该 task 调用已结束：从待认领队列里摘掉，避免被后来的子图误认领。"""
        cid = str(call_id or "")
        self._finished.add(cid)
        if cid in self._unclaimed:
            self._unclaimed.remove(cid)

    def resolve(self, ns_id: str) -> str:
        """命名空间 id → 委派 id（稳定、可重复调用）。"""
        nid = str(ns_id or "")
        if not nid:
            return ""
        hit = self._alias.get(nid)
        if hit:
            return hit
        # 首次见到这个命名空间：认领最早那个还没被认领的 task 调用
        for cid in list(self._unclaimed):
            if cid in self._finished:
                self._unclaimed.remove(cid)
                continue
            self._alias[nid] = cid
            self._unclaimed.remove(cid)
            return cid
        self._alias[nid] = nid  # 兜底：不丢事件
        return nid

    def snapshot(self) -> dict:
        """调试用：当前的认领状态。"""
        return {
            "alias": dict(self._alias),
            "unclaimed": list(self._unclaimed),
            "finished": sorted(self._finished),
        }



def subagent_timeout_seconds() -> int:
    """读取配置（测试里可以直接改 settings 覆盖）。"""
    from app.core.config import settings

    try:
        return int(getattr(settings, "subagent_timeout_seconds", 900) or 0)
    except (TypeError, ValueError):
        return 900


def wrap_subagent_timeout(original):  # noqa: ANN001, ANN201
    """给 deepagents 的 `task` 工具套超时，返回新的 coroutine 函数。

    为什么在工具外面套而不是在 runtime 里套：`task` 是**一次调用**，
    `asyncio.wait_for` 正好给它一个明确的边界；超时后抛出的异常会被
    LangGraph 的工具节点正常捕获成一次"工具失败"，模型能据此改策略。
    """
    async def timed_atask(description: str, subagent_type: str, runtime):  # noqa: ANN001
        limit = subagent_timeout_seconds()
        if limit <= 0:
            return await original(description, subagent_type, runtime)
        try:
            return await asyncio.wait_for(
                original(description, subagent_type, runtime), timeout=limit
            )
        except asyncio.TimeoutError as e:
            raise SubagentTimeoutError(
                f"子智能体 `{subagent_type}` 超过 {limit} 秒仍未结束，已中止本次委派。"
                f"建议：把任务拆小一些，或让它缩小检索范围后重试。"
            ) from e

    timed_atask.__name__ = getattr(original, "__name__", "atask")
    return timed_atask


def _iter_bound_tools(agent) -> list:  # noqa: ANN001
    """枚举 tools 节点上**真正会被执行**的工具对象（兼容多种存放形状）。

    为什么需要：旧实现写死了 `agent.nodes["tools"].bound._tools`，而实测
    （langgraph 1.x + deepagents 0.7.1）该 bound 是 `ToolNode`，工具存放在
    `_tools_by_name`（dict），**根本没有 `_tools`** → 查找失败 → 静默返回 False
    → 超时从未生效。这个"看起来做了、实际没做"的坑很难从现象上发现，所以这里
    把 langgraph 几代实现里出现过的容器都试一遍，并且允许从公开属性
    `tools_by_name` 兜底。
    """
    nodes = getattr(agent, "nodes", None)
    node = nodes.get("tools") if nodes is not None else None
    found: list = []
    seen: set[int] = set()
    # ToolNode 本体、以及被 RunnableBinding 包一层的情况都要覆盖
    for holder in (getattr(node, "bound", None), node):
        if holder is None:
            continue
        for attr in ("_tools_by_name", "tools_by_name", "_tools", "tools"):
            got = getattr(holder, attr, None)
            values = got.values() if isinstance(got, dict) else got
            if not isinstance(values, (list, tuple, type({}.values()))):
                continue
            for t in values:
                if id(t) in seen:
                    continue
                seen.add(id(t))
                found.append(t)
    return found


def apply_subagent_timeout(agent) -> bool:  # noqa: ANN001
    """把超时包装到 agent 内建的 `task` 工具上（幂等）。

    deepagents 的 `task` 工具由 `SubAgentMiddleware` 在中间件构造时生成，
    最终挂在 `tools` 节点的 `ToolNode` 上（见 `_iter_bound_tools`）。
    这里就地替换它的 `coroutine`，从而给每次委派加上时限。

    Returns:
        True 表示 `task` 工具**已带上超时**（本次新套上，或之前就套过）；
        False 表示没找到 `task` 工具或包装出错 —— 此时这轮**没有超时保护**，
        调用方必须把这件事说出来，否则又回到"超时只是个摆设"的老问题。
    """
    try:
        for t in _iter_bound_tools(agent):
            if getattr(t, "name", "") != "task":
                continue
            original = getattr(t, "coroutine", None)
            if original is None:
                logger.warning("[subagent] task 工具没有 coroutine，无法套超时")
                return False
            if getattr(original, "_harness_timed", False):
                return True  # 已经是目标状态，幂等
            wrapped = wrap_subagent_timeout(original)
            wrapped._harness_timed = True  # type: ignore[attr-defined]
            t.coroutine = wrapped
            logger.info(
                "[subagent] 已为 task 工具加上超时上限 %s 秒", subagent_timeout_seconds()
            )
            return True
        logger.warning(
            "[subagent] 没在 tools 节点里找到 task 工具，本轮**没有**超时保护"
        )
    except Exception as e:  # 包装失败不能影响主流程
        logger.warning("[subagent] 超时包装失败（不影响运行）: %s", e)
    return False


def streaming_astream(agent, agent_input, config) -> Any:  # noqa: ANN001
    """带 `subgraphs=True` 的事件流（子智能体内部事件因此可见）。

    统一把 langgraph 的两种分片形状归一成 `(namespace, message, metadata)`：
    - `subgraphs=True` 时为 `(namespace_tuple, (message, metadata))`
    - 否则为 `(message, metadata)`
    """
    return agent.astream(
        agent_input,
        config,
        stream_mode="messages",
        subgraphs=True,
    )


def normalize_chunk(chunk: Any) -> tuple[Any, Any, dict]:
    """把 astream 分片归一成 `(namespace, message, metadata)`。"""
    if isinstance(chunk, tuple) and len(chunk) == 2:
        first, second = chunk
        # (namespace, (message, metadata))
        if isinstance(second, tuple) and len(second) == 2:
            msg, meta = second
            return first, msg, (meta or {})
        # (message, metadata)
        return None, first, (second or {}) if isinstance(second, dict) else {}
    return None, chunk, {}


def log_usage_event(namespace: Any, msg: Any, limit: int = 40) -> dict | None:
    """把分片上的 `usage_metadata` 转成前端可用的用量事件；没有则返回 None。

    只统计**主智能体**的步（子智能体的用量单独标出来，避免混进主速率）。
    """
    usage = getattr(msg, "usage_metadata", None)
    if not usage:
        return None
    try:
        out = int(usage.get("input_tokens") or 0)
        oout = int(usage.get("output_tokens") or 0)
    except (TypeError, ValueError):
        return None
    sub = parse_subagent_namespace(namespace)
    details = usage.get("output_token_details") or {}
    try:
        reasoning = int(details.get("reasoning") or 0)
    except (TypeError, ValueError):
        reasoning = 0
    return {
        "input": out,
        "output": oout,
        "reasoning": reasoning,
        "total": int(usage.get("total_tokens") or (out + oout)),
        "scope": "subagent" if sub else "main",
        "subagent_id": sub or "",
        "model": str((getattr(msg, "response_metadata", None) or {}).get("model_name") or ""),
        "limit": limit,
    }


def tool_result_preview(content: Any, limit: int = 240) -> str:
    """子智能体内部工具结果的预览文本（只用于过程展示，不落库）。"""
    if isinstance(content, str):
        s = content
    else:
        try:
            s = str(content)
        except Exception:
            return ""
    s = " ".join(s.split())
    return s if len(s) <= limit else s[: limit - 1] + "…"


#: 推理增量所在字段（DeepSeek 推理模型放这里，而不是 content）
REASONING_KEY = "reasoning_content"


def reasoning_delta(msg: Any) -> str:
    """取一个分片里的**推理**增量文本（没有则空串）。

    为什么必须单独取：`content` 只在思考结束、开始写正文时才产出。若只转发
    `content`，模型"想"多久，前端就黑屏多久 —— 用户无法区分"在思考"和"挂了"。
    """
    ak = getattr(msg, "additional_kwargs", None) or {}
    v = ak.get(REASONING_KEY)
    if isinstance(v, str):
        return v
    # 少数实现放在 content blocks 里（dict 列表），这里一并兼容
    if isinstance(v, list):
        return "".join(str(p.get("text", "")) for p in v if isinstance(p, dict))
    return ""


async def iter_subagent_events(namespace: Any, msg: Any) -> AsyncIterator[tuple[str, dict]]:
    """把子智能体的内部分片翻译成前端事件。

    产出：
      - ("subagent_token",    {"id", "delta"})   子智能体产出的正文增量
      - ("subagent_thinking", {"id", "delta"})   子智能体的推理增量（思考中）
      - ("subagent_tool",     {"id", "name", "args", "result", "status"}) 它自己的工具调用
    """
    call_id = parse_subagent_namespace(namespace)
    if not call_id:
        return

    from langchain_core.messages import ToolMessage

    if isinstance(msg, ToolMessage):
        yield ("subagent_tool", {
            "id": call_id,
            "name": str(getattr(msg, "name", "") or ""),
            "args": {},
            "result": tool_result_preview(getattr(msg, "content", "")),
            "status": "done",
        })
        return

    for tc in getattr(msg, "tool_calls", None) or []:
        if isinstance(tc, dict) and tc.get("name"):
            yield ("subagent_tool", {
                "id": call_id,
                "name": str(tc["name"]),
                "args": tc.get("args") or {},
                "result": "",
                "status": "running",
            })

    think = reasoning_delta(msg)
    if think:
        yield ("subagent_thinking", {"id": call_id, "delta": think})

    content = getattr(msg, "content", "")
    if isinstance(content, str) and content:
        yield ("subagent_token", {"id": call_id, "delta": content})


__all__ = [
    "REASONING_KEY",
    "SubagentCallRegistry",
    "SubagentTimeoutError",
    "apply_subagent_timeout",
    "iter_subagent_events",
    "log_usage_event",
    "normalize_chunk",
    "parse_subagent_namespace",
    "reasoning_delta",
    "streaming_astream",
    "subagent_timeout_seconds",
    "summarize_task",
    "tool_result_preview",
    "wrap_subagent_timeout",
]
