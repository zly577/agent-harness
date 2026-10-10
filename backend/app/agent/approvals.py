"""人工介入审批核心。

利用 deepagents 内置的 HITL 机制：
- 通过 `permissions=[FilesystemPermission(mode="interrupt", ...)]`，deepagents 自动把
  敏感路径操作（如写 /system/、/admin/）变成审批中断。
- 运行时中断后，graph 处于 pending 状态，可从 checkpoint 读取待审批的 action_requests。
- 审批通过/拒绝后，用 `Command(resume=HITLResponse)` 恢复 graph 继续执行。

本模块提供：
- read_pending_approval(agent, config)：读取一条会话当前**全部**待审批项（含中断 id）。
- build_resume_map(interrupts, approve)：构造多中断恢复映射。
- build_decision(actions, approve)：单中断场景的扁平决策列表（兜底）。
- 供 FastAPI 审批路由调用（见 app/api/routes/approvals.py）。

说明：HITLResponse.decisions 与参与中断的 tool_call 一一对应；
approve → 继续执行；reject → 跳过该工具。

## 一个会话可以同时挂多个中断

这是本模块最容易踩的坑。中断由 deepagents 的 HITL 中间件在**工具节点**触发，
而"并行"在这个系统里很常见：主智能体在同一个 superstep 里并行委派多个
子智能体（`tool_calls=['task','task','task',...]`），每个子智能体内部再写文件，
于是每次写文件都挂一个中断 —— 实测见过 5 个中断同时挂着。

这类情况下恢复必须走**映射形态** `{interrupt_id: resume_value}`；
用扁平的 `{"decisions": [...]}` 会被 langgraph 直接拒绝：

    RuntimeError: When there are multiple pending interrupts, you must specify
    the interrupt id when resuming.

（langgraph 用"键是否都是 xxh3_128 摘要"来区分映射形态，而 `Interrupt.id`
恰好就是这种摘要，所以直接拿它当键。）
"""
from __future__ import annotations

import json
import logging
from typing import Any

from langgraph.types import Command

logger = logging.getLogger(__name__)

#: 进程内登记：**这一轮运行里见过**"确实挂着中断"的会话 → 归属用户。
#:
#: 为什么需要它：关闭人工介入时要"把挂起的中断收尾"，而收尾需要先知道**找谁收**。
#: 前端只知道自己内存里记过弹窗的那几个会话（刷新过、切过页面就漏），所以后端要
#: 自己留一份。它只在 `read_pending_approval` 读到 pending 时登记，容量以"本进程
#: 期间用户真正碰过、且真的被中断过的会话"为界，不会无限膨胀。
#:
#: 值从 `set[str]` 改成 `dict[session_id, user_id]`。
#: 原因很实在：多租户下 `clear_pending_interrupt(None)` 会把**所有人**挂起的审批
#: 一次性 reject 掉 —— 我关自己的审批开关，别人正在等的审批就没了。
#: 记下归属之后，收尾时可以按用户过滤（见 `known_pending_sessions`）。
_pending_sessions: dict[str, str | None] = {}


def known_pending_sessions(user_id: str | None = None) -> list[str]:
    """返回本进程期间登记过"挂着待审批中断"的会话 id。

    Args:
        user_id: 只返回该用户的会话。传空 = 全部（进程级收尾/诊断用）。
    """
    if user_id is None:
        return sorted(_pending_sessions)
    return sorted(sid for sid, uid in _pending_sessions.items() if uid == user_id)


def forget_pending(session_id: str | None) -> None:
    """把某会话从登记表里摘掉（中断确实被处理掉了）。"""
    if session_id:
        _pending_sessions.pop(session_id, None)


def _note_pending(session_id: str | None, user_id: str | None = None) -> None:
    if not session_id:
        return
    # 已有归属就不要被后来的 None 覆盖（同会话先有主、后有探针读取的情况）
    if user_id or session_id not in _pending_sessions:
        _pending_sessions[session_id] = user_id


def _summarize_args(args: Any, *, max_items: int = 6, max_len: int = 160) -> str:
    """把审批参数压成单行摘要字符串。

    只用于展示：审批弹窗要求"小、紧凑"，长命令 / 长文件内容不能撑爆弹窗
    （否则同意按钮会被挤出可视区）。前端主要用 args 做键值展示，这个摘要
    只是给 GET 接口和日志一个可直接读的短文本。
    """
    def one(v: Any) -> str:
        if isinstance(v, str):
            s = v
        elif v is None or isinstance(v, (int, float, bool)):
            s = json.dumps(v, ensure_ascii=False)
        else:
            try:
                s = json.dumps(v, ensure_ascii=False)
            except Exception:
                s = str(v)
        s = " ".join(s.split())
        return s if len(s) <= max_len else s[:max_len] + "…"

    if isinstance(args, dict):
        parts = [f"{k}={one(v)}" for k, v in list(args.items())[:max_items]]
        rest = len(args) - len(parts)
        if rest > 0:
            parts.append(f"…(+{rest})")
        return "; ".join(parts)
    if isinstance(args, list):
        parts = [one(v) for v in args[:max_items]]
        if len(args) > len(parts):
            parts.append("…")
        return ", ".join(parts)
    return one(args)


def _action_error_text(value: Any) -> str:
    """把 HITL 中断里携带的错误信息归一为短文本。"""
    if isinstance(value, str):
        return value[:500]
    if value is None:
        return ""
    try:
        return json.dumps(value, ensure_ascii=False)[:500]
    except Exception:
        return str(value)[:500]


def _actions_of(value: Any) -> list[dict]:
    """把一个中断 value（HITLRequest）里的 action_requests 归一成前端可展示的列表。

    字段来源是 langchain HITL 的 `ActionRequest`：顶层就是 `name` / `args` /
    `description`（另可能带 `error`），**不是** `tool_call.name` —— 早期实现按
    `ar["tool"]` 取值，导致前端拿到的工具名恒为空。
    """
    actions: list[dict] = []
    if not isinstance(value, dict):
        return actions
    for ar in value.get("action_requests", []):
        if not isinstance(ar, dict):
            continue
        # 兼容两种形状：ActionRequest 顶层字段，以及嵌套的 tool_call
        nested = ar.get("tool_call") if isinstance(ar.get("tool_call"), dict) else {}
        name = ar.get("name") or ar.get("tool") or nested.get("name") or ""
        args = ar.get("args")
        if args is None:
            args = nested.get("args")
        if not isinstance(args, (dict, list)):
            args = {} if args is None else {"value": args}
        actions.append({
            "type": ar.get("type") or "tool_call",
            "tool": str(name or ""),
            "args": args,
            "summary": _summarize_args(args),
            "description": str(ar.get("description") or "")[:500],
            "error": _action_error_text(ar.get("error")),
        })
    return actions


async def read_pending_approval(agent, config: dict, user_id: str | None = None) -> dict | None:
    """读取该会话当前待审批的中断信息（如有）。

    返回形如:
        {"thread_id": ..., "pending": True,
         "actions":     [ {type, tool, args, summary, description, error} ],
         "interrupts":  [ {"id": <中断 id>, "actions": [...]} ]}
    无中断时返回 {"pending": False}。

    ## 为什么必须返回**全部**中断（而不是只取第一个）

    HITL 中断是按需挂起的，一个会话**可以同时有多个**。实测案例：主智能体在同一个
    superstep 里并行委派了 5 个子智能体，5 个子智能体各自 `write_file`，
    于是 checkpoint 里同时躺着 5 个中断（part1.md ~ part5.md）。

    只读 `interrupts[0]` 会带来两个连环故障：
    1. 弹窗只显示 5 个待审批操作里的 1 个 —— 用户根本不知道还有 4 个在等；
    2. 恢复时用 `Command(resume={"decisions": [...]})`（不带 id），langgraph 直接
       抛 `RuntimeError: When there are multiple pending interrupts, you must
       specify the interrupt id when resuming`。

    第 2 条的表现极具误导性：点"同意/拒绝"后弹窗消失又立刻弹回来（路由把错误
    回吐成 error 事件、前端又把弹窗放回去），用户看到的就是"点什么都无效"。
    所以这里把每个中断连同它的 id 一起带出去，供 `build_resume_map` 组恢复映射。

    ## 判据：`interrupts` **且** `next` 非空

    只看 `state.interrupts` 会误报。`Snapshot.interrupts` 是"checkpoint 里的待跑任务"
    到**当前这份图**的 nodes 里逐个匹配出来的，所以两种情况下它会给出假阳性：

    1. **resume map 形态恢复之后**，langgraph 不写 per-task `RESUME` 记录，已经处理完的
       中断仍留在 `Snapshot.interrupts` 里 —— 弹窗会再次列出**已经写完的文件**；
    2. **人工介入被关掉后** HITL 节点从图里消失，同一份 checkpoint 又反过来读不出中断
       （点"同意"只会得到 409「无待审批项」，用户看到"已审批过"），再打开时它又冒出来。

    `state.next` 是"还没有跑完的节点"（langgraph 语义：没有常规写入的 pending task）。
    图真的停在某个审批上时它必然非空；跑到底时为空。拿它当第二判据，
    上面第 1 类假阳性就被排除了。

    第 2 类不再出现的原因是**图结构已经统一**（关闭人工介入也保留 HITL 中间件，
    见 `app/agent/permissions.py`）。

    Args:
        user_id: 会话归属者。只用于**登记**（关闭开关时按用户收尾），
            不参与"是否 pending"的判断 —— 那个判断只看 checkpoint。
    """
    state = await agent.aget_state(config)
    # 中断由 HITLMiddleware 在工具节点触发后，next 指向 tools；interrupt value 即 HITLRequest
    interrupts = getattr(state, "interrupts", None) or []
    if not interrupts or not (getattr(state, "next", None) or ()):
        return {"pending": False}

    groups: list[dict] = []
    actions: list[dict] = []
    for inter in interrupts:
        value = inter.value if hasattr(inter, "value") else inter
        group_actions = _actions_of(value)
        groups.append({
            "id": str(getattr(inter, "id", "") or ""),
            "actions": group_actions,
        })
        actions.extend(group_actions)

    # 登记：关闭人工介入时要靠这份名单把挂起的中断逐个收尾
    _note_pending(config.get("configurable", {}).get("thread_id"), user_id)

    return {
        "thread_id": config.get("configurable", {}).get("thread_id"),
        # pending 以**中断是否存在**为准：即使某个中断的 value 形状不认识
        # （拿不到 action 明细），它也确实挂着，必须让用户能处理掉。
        "pending": True,
        "actions": actions or [{
            "type": "interrupt",
            "tool": "",
            "args": {},
            "summary": "未知类型的待审批操作",
            "description": "",
            "error": "",
        }],
        "interrupts": groups,
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
    """为所有待审批 action 构造 approve/reject 决策列表。

    单中断场景用的**扁平静态列表**。多中断请用 `build_resume_map` ——
    扁平列表交给 `Command(resume=...)` 会被 langgraph 拒绝（见其文档字符串）。
    """
    count = len(actions) if actions else 1
    return [{"type": "approve" if approve else "reject"} for _ in range(count)]


def build_resume_map(interrupts: list[dict] | None, approve: bool) -> dict[str, dict]:
    """构造 langgraph 的**多中断恢复映射**：`{interrupt_id: {"decisions": [...]}}`。

    为什么必须是映射：一个会话可以同时挂多个中断（并行委派的子智能体各自触发审批）。
    这种情况下 `Command(resume={"decisions": [...]})` 会抛
    `RuntimeError: When there are multiple pending interrupts, you must specify
    the interrupt id when resuming`。

    langgraph 通过"键是否都是 xxh3_128 十六进制摘要"来区分
    "映射形态"与"普通值形态"（见 `langgraph/pregel/_loop.py`），
    `Interrupt.id` 正好就是这种摘要，所以直接用它的 id 作键。

    每个中断下有几个 action，就给它几个同样决策 —— 与弹窗"同意/拒绝"
    这对整体按钮的语义一致（用户看到的是 N 个操作，点一次 = 对 N 个都生效）。

    缺失 id 的中断会被跳过（拿不到 id 就无法定向恢复）；这种情况调用方应回退到
    `build_decision`。
    """
    resume: dict[str, dict] = {}
    for group in interrupts or []:
        interrupt_id = str((group or {}).get("id") or "").strip()
        if not interrupt_id:
            continue
        count = len((group or {}).get("actions") or []) or 1
        resume[interrupt_id] = {
            "decisions": [{"type": "approve" if approve else "reject"} for _ in range(count)],
        }
    return resume
