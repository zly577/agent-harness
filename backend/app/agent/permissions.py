"""人工介入（HITL）的运行时热插拔开关。

## 为什么需要这个模块

deepagents 的审批中断是**编译期**决定的一次性配置：`create_deep_agent(permissions=[...])`
里只要有一条 `mode="interrupt"` 的规则，deepagents 就会自动挂上
`HumanInTheLoopMiddleware`（见 `deepagents/graph.py:872-876`）；反过来，权限为空列表时
压根不会挂这个中间件 —— 全程零中断。

也就是说"要不要人工介入"这件事只取决于 **构建 Agent 时传进去的 permissions**。
所以要热插拔，只需要：

1. 构建 Agent 前先问这里 `agent_permissions()` 拿权限（开 = 敏感写入要审批，关 = 返回空列表）；
2. 开关变化时把**已缓存的 Agent 丢掉**（`runtime.invalidate_agents()`），
   下次对话就会用新权限重建 —— 权限是绑在 graph 上的，不重建就换不掉。

## 语义

- 默认值来自配置 `HITL_ENABLED`（.env，默认 true = 保留审批）。
- `set_hitl(False)` → 该会话（或全局）后续对话**全程无中断**，本机访问直接执行。
- `set_hitl(True)` → 恢复"访问本机真实文件（`/mnt/**`）需人工同意"。

## 审批范围由「权限模式」和「HITL 开关」共同决定

| 权限模式（`app/agent/access.py`） | HITL 开关 | 实际行为 |
|---|---|---|
| `sandbox`（默认） | 开（默认） | 访问本机 `/mnt/**`（读 + 写）→ 弹审批；工作区 `/workspace/**` 自由 |
| `sandbox` | 关 | 全程不弹窗，本机访问直接执行 |
| `full`（完全访问） | 任意 | **一律不弹窗**（用户已显式选择"就在本机干活"） |

工作区 `/workspace/**` 不再需要审批 —— 它是"沙箱内的地盘"。
- 运行期改的是内存态（进程重启回到 .env 默认值），便于本机调试与演示；
  要跨重启保持，改 .env 即可。

## ⚠️ 关闭时**不能**让 HITL 中间件从图里消失（修复）

早期实现里 `agent_permissions(off)` 返回空列表，deepagents 于是**根本不挂**
`HumanInTheLoopMiddleware` —— 图少了一个节点。这件事的代价远超"少一次审批"：

中断是**存在 checkpoint 里**的（`__interrupt__` 写 + 对应的 pending task）。
langgraph 重建 `StateSnapshot.interrupts` 时，是拿 checkpoint 里的待跑任务**逐个到
当前这份图的 nodes 里找**（`prepare_next_tasks(... self.nodes ...)`）。HITL 节点没了，
那个中断任务就推不出来，于是同一份 checkpoint 出现两种互相矛盾的读数：

| 开关 | `state.next` | `state.interrupts` | 用户看到 |
|------|--------------|--------------------|----------|
| 需要审批 | `('HumanInTheLoopMiddleware.after_model',)` | 1 条 | 弹窗（正确） |
| 自动执行 | `()` | 0 条 | 弹窗还在，但点"同意"→ 409「无待审批项」→ 提示"已审批过" |
| 再切回审批 | `(...)` | **又是那同一条**（id 不变） | 弹窗"复活" |

也就是"自动执行后仍弹审批窗 / 点同意说已审批过"这对现象的全部来源。

所以现在关闭人工介入时 **仍然保留 HITL 中间件**，只把审批表置空
（`hitl_interrupt_on()` 给一个永不匹配的占位键）：图结构前后一致，
中断始终看得见、也始终恢复得动。真要"放弃"挂起的中断，走
`api/routes/approvals.clear_pending_interrupt()` 显式拒绝收尾，而不是靠它隐身。
"""
from __future__ import annotations

import logging

from app.core.config import settings

logger = logging.getLogger(__name__)

#: 关闭人工介入时用来"占位"的工具名。
#:
#: `HumanInTheLoopMiddleware` 只按 `self.interrupt_on.get(tool_call["name"])` 查表，
#: 表里有一个**永远不会被真实工具命中**的键，就能让 deepagents 认为
#: "有审批配置"从而挂上中间件（`_merge_fs_interrupt_on` 在整体为空时返回 None，
#: 意味着不挂），而实际效果是全程放行。它的唯一作用是**保住图的结构**。
DISABLED_INTERRUPT_KEY = "__hitl_disabled__"

#: 全局默认开关；进程内可被 API 改写（初始值 = .env 的 HITL_ENABLED）。
#: 只有 admin 能改它（见 api/routes/hitl.py），普通用户改的是自己那一层。
_global_hitl: bool = bool(getattr(settings, "hitl_enabled", True))
#: 按用户覆盖：user_id → bool。
_user_hitl: dict[str, bool] = {}
#: 按会话覆盖：session_id → bool。没用到的会话跟随用户/全局默认。
_session_hitl: dict[str, bool] = {}


def hitl_enabled(session_id: str | None = None, user_id: str | None = None) -> bool:
    """该会话/该用户当前是否需要人工介入。

    优先级：会话覆盖 → 用户覆盖 → 全局默认。
    """
    if session_id and session_id in _session_hitl:
        return _session_hitl[session_id]
    if user_id and user_id in _user_hitl:
        return _user_hitl[user_id]
    return _global_hitl


def set_hitl(enabled: bool, session_id: str | None = None,
             user_id: str | None = None) -> bool:
    """打开/关闭人工介入。返回设置后的值。

    Args:
        enabled: True = 敏感操作需要人工同意；False = 全程自动执行。
        session_id: 指定则只影响该会话。
        user_id: 指定则只影响该用户（不改全局）。
        两者都不传 = 改全局默认（并清掉各用户/会话的覆盖，让所有人统一跟随）。

    作用域差异很重要：以前"不带 session_id"就是全局切换，任何用户点一下
    都会改掉所有人的默认值。现在全局那一层只能由 admin 改（路由层拦），
    普通用户传 user_id 只影响自己。
    """
    global _global_hitl
    if session_id:
        _session_hitl[session_id] = bool(enabled)
    elif user_id:
        _user_hitl[user_id] = bool(enabled)
    else:
        _global_hitl = bool(enabled)
        _user_hitl.clear()
        _session_hitl.clear()
    logger.info(
        "[hitl] 人工介入已%s（scope=%s）",
        "开启" if enabled else "关闭",
        session_id or (f"user:{user_id}" if user_id else "global"),
    )
    return bool(enabled)


def _interrupt_rules(session_id: str | None = None, user_id: str | None = None,
                     mode: str | None = None) -> list:
    """本次构建 Agent 实际要生效的审批规则。

    两个条件**同时**满足才有规则：

    1. 权限模式是 `sandbox`（完全访问 = 用户已显式选择"不问了"，直接放行）；
    2. HITL 开关是开的。

    返回空列表时**并不等于**图里没有 HITL 中间件 —— 见 `hitl_interrupt_on()`。
    """
    from app.agent import access

    resolved = access.normalize_mode(mode) if mode else access.access_mode(session_id, user_id)
    if resolved == access.MODE_FULL:
        return []
    if not hitl_enabled(session_id, user_id):
        return []
    from app.agent.sandbox import default_permissions

    return default_permissions(resolved)


def agent_permissions(session_id: str | None = None, user_id: str | None = None,
                      mode: str | None = None) -> list:
    """构建 Agent 时要传的 `permissions`。

    开启（sandbox 模式 + HITL 开）：返回 deepagents 的 interrupt 权限
        —— 访问本机真实文件 `/mnt/**` 时触发人工审批。
    关闭（完全访问模式，或 HITL 关）：返回空列表 —— 不拦任何工具。

    注意：这是一个**编译期**输入，改模式/开关后必须让 runtime 重建 Agent 才生效。
    """
    return _interrupt_rules(session_id, user_id, mode)


def hitl_interrupt_on(session_id: str | None = None, user_id: str | None = None,
                      mode: str | None = None) -> dict | None:
    """构建 Agent 时要传的 `interrupt_on`（决定"有没有审批表"）。

    有审批规则时：返回 None —— 审批表完全由 `permissions` 生成
        （deepagents 的 `_build_interrupt_on_from_permissions`），这里不要插一手。
    没有审批规则时（完全访问模式 / HITL 关 / 没有本机挂载点）：返回
        `{DISABLED_INTERRUPT_KEY: False}` —— 一个永不匹配的占位键，
        **唯一的目的是让 `HumanInTheLoopMiddleware` 仍然被装配进图**。

    ⚠️ 别把它改成"没有规则就返回 None"：`_merge_fs_interrupt_on` 在整体为空时返回
    None，等于不挂中间件，图就少一个节点 —— 已挂起的中断会因此"隐身 / 复活"，
    详见模块头注释里那张对照表。

    ⚠️ 特别提醒：`HARNESS_MACHINE_ACCESS=false` 时 `default_permissions()` 返回
    **空列表**（没有本机路径可审批），所以"空规则 ⇒ 也要挂中间件"这条现在真的会被
    走到 —— 以前总有一条 `/workspace/**` 兜着，不会暴露这个问题。
    """
    if _interrupt_rules(session_id, user_id, mode):
        return None
    return {DISABLED_INTERRUPT_KEY: False}


def hitl_state(session_id: str | None = None, user_id: str | None = None) -> dict:
    """给前端/接口看的开关状态。"""
    return {
        "enabled": hitl_enabled(session_id, user_id),
        "scope": "session" if (session_id and session_id in _session_hitl)
                 else ("user" if (user_id and user_id in _user_hitl) else "global"),
        "global_default": _global_hitl,
        "user_overrides": dict(_user_hitl),
        "session_overrides": dict(_session_hitl),
    }
