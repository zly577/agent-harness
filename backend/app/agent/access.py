"""权限模式：**默认权限（沙箱内）** vs **完全访问权限**。

## 两种模式到底差在哪

| | 默认权限 `sandbox` | 完全访问 `full` |
|---|---|---|
| 文件读写 | 本地工作区 `/workspace/**` + 本机挂载点 `/mnt/<盘>/**` | 同左（共用同一层路径语义） |
| `execute` | OpenSandbox **容器**（隔离，执行前后做文件同步） | **本机 shell**（`subprocess`，无隔离、无同步） |
| 访问本机 `/mnt/**` | 读 / 写都要**人工审批**（HITL 开时） | 不审批，直接执行 |
| 实现 | `sandbox.build_main_backend(mode="sandbox")` → `HybridDefaultBackend` | 同函数 `mode="full"` → `HostShellBackend`（deepagents `LocalShellBackend`） |

一句话：**默认权限** = "在沙箱里干活，要碰你电脑上的东西先问我"；
**完全访问** = "就在你这台电脑上干活，不问了"。

## 为什么是"编译期"输入

模式决定 `create_deep_agent(backend=...)` 与 `permissions=...` 两个**构建期参数**，
所以切换时必须丢缓存重建 Agent（`runtime.invalidate_agents()`），
和 HITL 开关（见 `permissions.py`）是同一套机制。

## 作用域与持久性

优先级 **会话 → 用户 → 全局默认**，与 HITL 开关保持一致的语义：

- 全局默认来自配置 `HARNESS_ACCESS_MODE`（.env，默认 `sandbox`）；
- 运行期只改内存态（进程重启回到 .env 默认值）。这是刻意的：
  **完全访问是个危险档位，重启后自动回落**，不要让它"悄无声息地一直开着"。
  同理，只有 admin 能改全局那一层（见 `api/routes/access.py`）。

## 与 HITL 开关的关系

两者正交但互相影响：

- `mode=full` ⇒ **无论 HITL 开关怎么设都不审批**（用户已经显式选择了完全访问）；
- `mode=sandbox` + `hitl_enabled=False` ⇒ 也不审批（保留 HITL 中间件但不拦任何工具）；
- `mode=sandbox` + `hitl_enabled=True` ⇒ 只有访问本机 `/mnt/**` 才弹审批。
"""
from __future__ import annotations

import logging

from app.core.config import settings

logger = logging.getLogger(__name__)

#: 默认权限：文件落工作区、execute 进沙箱、碰本机要审批。
MODE_SANDBOX = "sandbox"
#: 完全访问：文件与命令都直接落本机（deepagents `LocalShellBackend`），不审批。
MODE_FULL = "full"

VALID_MODES: tuple[str, ...] = (MODE_SANDBOX, MODE_FULL)

#: 给前端/日志用的中文名。
MODE_LABELS: dict[str, str] = {
    MODE_SANDBOX: "默认权限（沙箱内）",
    MODE_FULL: "完全访问权限（本机）",
}


def normalize_mode(value: str | None) -> str:
    """把外部传进来的模式字符串归一化；无法识别时抛 `ValueError`。

    ⚠️ 这里刻意**不**静默回落到默认值：一个拼错的模式名（比如 `"local"`）如果被
    悄悄当成 `sandbox`，用户会以为"完全访问没生效"，而不是"我写错了"。
    不过要注意方向 —— 回落一定是往**安全**那边，所以真正兜底在 `access_mode()`
    里（配置值非法 → sandbox），而 API 入参非法 → 400。
    """
    text = (value or "").strip().lower()
    if text == MODE_FULL and not settings.harness_allow_full_access:
        raise ValueError("此部署已禁用完全访问模式，请使用 OpenSandbox 执行代码。")
    if text in VALID_MODES:
        return text
    raise ValueError(f"未知的权限模式：{value!r}（可选：{' / '.join(VALID_MODES)}）")


#: 全局默认（初始值 = .env 的 HARNESS_ACCESS_MODE；配置非法时回落到 sandbox）。
_global_mode: str = (
    settings.harness_access_mode
    if (settings.harness_access_mode or "").strip().lower() in VALID_MODES
    else MODE_SANDBOX
)
#: 按用户覆盖：user_id → mode
_user_mode: dict[str, str] = {}
#: 按会话覆盖：session_id → mode
_session_mode: dict[str, str] = {}


def access_mode(session_id: str | None = None, user_id: str | None = None) -> str:
    """该会话/该用户当前的权限模式。优先级：会话 → 用户 → 全局默认。"""
    if not settings.harness_allow_full_access:
        return MODE_SANDBOX
    if session_id and session_id in _session_mode:
        return _session_mode[session_id]
    if user_id and user_id in _user_mode:
        return _user_mode[user_id]
    return _global_mode


def set_access_mode(mode: str, session_id: str | None = None,
                    user_id: str | None = None) -> str:
    """切换权限模式，返回设置后的值。

    Args:
        mode: `"sandbox"` 或 `"full"`；其它值抛 `ValueError`。
        session_id: 指定则只影响该会话。
        user_id: 指定则只影响该用户（不改全局）。
        两者都不传 = 改全局默认，并清掉各用户/会话的覆盖（让所有人统一跟随）。

    Returns:
        归一化后的模式字符串。
    """
    global _global_mode
    resolved = normalize_mode(mode)
    if session_id:
        _session_mode[session_id] = resolved
    elif user_id:
        _user_mode[user_id] = resolved
    else:
        _global_mode = resolved
        _user_mode.clear()
        _session_mode.clear()
    logger.info(
        "[access] 权限模式已切换为 %s（%s，scope=%s）",
        resolved,
        MODE_LABELS.get(resolved, resolved),
        session_id or (f"user:{user_id}" if user_id else "global"),
    )
    return resolved


def access_state(session_id: str | None = None, user_id: str | None = None) -> dict:
    """给前端/接口看的模式状态。"""
    current = access_mode(session_id, user_id)
    return {
        "mode": current,
        "label": MODE_LABELS.get(current, current),
        "full_access": current == MODE_FULL,
        "scope": "session" if (session_id and session_id in _session_mode)
                 else ("user" if (user_id and user_id in _user_mode) else "global"),
        "global_default": _global_mode,
        "user_overrides": dict(_user_mode),
        "session_overrides": dict(_session_mode),
        "valid_modes": list(VALID_MODES) if settings.harness_allow_full_access else [MODE_SANDBOX],
    }


__all__ = [
    "MODE_FULL",
    "MODE_LABELS",
    "MODE_SANDBOX",
    "VALID_MODES",
    "access_mode",
    "access_state",
    "normalize_mode",
    "set_access_mode",
]
