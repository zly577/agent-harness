"""子智能体注册表。

从 `configs/subagents/*.yaml` 加载子智能体定义，经 Pydantic 校验后构建 deepagents `SubAgent`。

关键：**子智能体不传 model → 继承主 Agent 的模型**（官方确认），因此这里用同一个 DeepSeek。
隔离：deepagents 的 `task` 工具让子任务运行在 **isolated context window**（上下文隔离）；
工具/权限隔离通过 `permissions` 实现。

## 重要约束：子智能体的权限路径必须落在 route 前缀内

`create_deep_agent` 会为**每个子智能体**单独构造一个 `FilesystemMiddleware`，用的是
**父级的 backend**（`graph.py:668`）。父级 backend 具备执行能力
（`SandboxBackendProtocol`），于是 deepagents 的这条校验对子智能体同样生效：

    所有权限路径（paths）都必须以某个 route 前缀开头，否则 NotImplementedError

所以子智能体配置里写的都是**工作区内的相对路径**（如 `/notes/`），
由 `build_subagent()` 统一加上 `/workspace` 前缀 + 递归通配 `**`
（`/notes/` → `/workspace/notes/**`，`/` → `/workspace/**`），
与主 Agent 的 `PERMISSION_ROUTE_PREFIX = "/workspace/"` 对齐。

，本机磁盘挂载点（`/mnt/<名称>/...`）也是合法 route 前缀：配置里直接写
`/mnt/d/proj` 即可授权该目录（`_to_route_path()` 会识别已带前缀的写法，不再叠
`/workspace`）。注意它只在 `HARNESS_MACHINE_ACCESS=true` 时才存在 route —— 否则
这条校验会失败，`validate_permissions()` 会在启动期把问题说清楚。
"""
from functools import lru_cache
from pathlib import Path

import yaml
from pydantic import BaseModel, Field, field_validator

from app.core.config import settings

_CONFIG_DIR = Path(__file__).resolve().parent.parent.parent / "configs" / "subagents"

#: 工作区前缀（= 容器内工作目录 = 模型看到的路径前缀），与 sandbox.py 保持一致。
WORKSPACE_PREFIX: str = (settings.sandbox_root or "/workspace").rstrip("/") or "/workspace"


class SubAgentConfig(BaseModel):
    """一个 YAML 子智能体定义。"""

    name: str
    description: str
    system_prompt: str
    # 可选：指定工具。为空则继承主 Agent 的默认工具（文件工具等）。
    tools: list[str] = Field(default_factory=list, description="工具名白名单，空=继承默认")
    # 可选：文件权限（**工作区内相对路径**，如 /notes/；会在构建时补上 /workspace 前缀）
    permissions_allow_read: list[str] = Field(default_factory=list, description="允许读的路径前缀")
    permissions_allow_write: list[str] = Field(default_factory=list, description="允许写的路径前缀")

    @field_validator("name")
    @classmethod
    def _name_valid(cls, v: str) -> str:
        # 子代理名需可作为 task 工具入参，用安全字符
        if not v or any(not (c.isalnum() or c in "_-") for c in v):
            raise ValueError("name 只能含字母/数字/下划线/连字符")
        return v

    @field_validator("system_prompt")
    @classmethod
    def _prompt_valid(cls, v: str) -> str:
        if not v.strip():
            raise ValueError("system_prompt 不能为空")
        return v


def _route_prefixes() -> list[str]:
    """当前所有可用的 route 前缀（权限路径必须落在其中之一，否则 deepagents 报错）。

    - `/workspace/` = 工作区（本地磁盘 + 沙箱镜像）
    `/mnt/<名>/` = 本机磁盘挂载点（HARNESS_MACHINE_ACCESS=false 时为空）
    """
    from app.agent.opensandbox_backend import machine_mount_prefix, machine_mounts

    return [WORKSPACE_PREFIX + "/"] + [machine_mount_prefix(n) for n, _ in machine_mounts()]


def _to_route_path(raw: str) -> str:
    """工作区内相对路径 → 落在 route 前缀内的 glob 路径。

    | 配置里写的 | 实际权限路径        | 含义                     |
    |-----------|--------------------|--------------------------|
    | `/`       | `/workspace/**`    | 整个工作区                |
    | `/notes/` | `/workspace/notes/**` | 工作区下的 notes 子目录 |
    | `/workspace/x` | `/workspace/x/**` | 已带前缀，只补通配     |
    | `/mnt/d/proj` | `/mnt/d/proj/**` | 本机 D 盘的 proj 目录 |

    判据：**是否已经以某个 route 前缀开头**（工作区 / 本机挂载点）；都不是就当成
    工作区相对路径。带 `/mnt/...` 的写法只在 `HARNESS_MACHINE_ACCESS=true` 时才有对应
    route —— 否则 deepagents 会因"权限路径不在任何 route 内"抛 NotImplementedError。
    """
    path = (raw or "").strip()
    if not path:
        return path
    if path == "/":
        return WORKSPACE_PREFIX + "/**"

    for prefix in _route_prefixes():
        base = prefix.rstrip("/")
        if path == base or path.startswith(prefix):
            inner = path[len(base):].strip("/")
            if not inner:
                return prefix + "**"
            # 目录式路径补 `**` 递归匹配（deepagents 的 glob 语义：目录结尾不匹配子孙）
            return prefix + (inner if inner.endswith("**") else inner + "/**")

    inner = path.strip("/")
    # 目录式路径补 `**` 递归匹配；已带通配的不重复叠加
    return f"{WORKSPACE_PREFIX}/{inner}" + ("" if inner.endswith("**") else "/**")


def load_all_subagents_cfg() -> list[SubAgentConfig]:
    """读取 configs/subagents/ 下所有 YAML，做 Pydantic 校验。"""
    result: list[SubAgentConfig] = []
    if not _CONFIG_DIR.exists():
        return result
    seen: set[str] = set()
    for f in sorted(_CONFIG_DIR.glob("*.yaml")):
        try:
            data = yaml.safe_load(f.read_text(encoding="utf-8")) or {}
            cfg = SubAgentConfig(**data)
        except Exception as e:
            raise RuntimeError(f"子智能体配置 {f.name} 校验失败: {e}") from e
        if cfg.name in seen:
            raise RuntimeError(f"子智能体 name 重复: {cfg.name}")
        seen.add(cfg.name)
        result.append(cfg)
    return result


def build_subagent(cfg: SubAgentConfig, *, workspace: Path | None = None, mode: str = "sandbox", backend=None,
                   context_model=None, preferences=None, session_id=None, inherited_tools=None):
    """把配置构建为 deepagents SubAgent（省略 model 以继承主 Agent 模型）。

    权限路径会经 `_to_route_path()` 统一到 `/workspace` 前缀内 —— 这是硬性要求：
    子智能体的 `FilesystemMiddleware` 用的是父级（可执行）backend，deepagents 要求
    所有权限路径都落在某个 route 前缀内，否则直接抛 NotImplementedError。
    """
    from app.agent.clarification import CLARIFICATION_POLICY, ClarificationMiddleware
    from deepagents import SubAgent
    from deepagents.middleware.filesystem import FilesystemPermission

    from app.agent.middleware import build_intent_strip_middleware, build_self_kill_guard_middleware
    from app.agent.task_state import TaskStateMiddleware

    system_prompt = cfg.system_prompt + "\n\n" + CLARIFICATION_POLICY
    if workspace is not None:
        from app.agent.prompt import current_workspace_prompt_section
        system_prompt += current_workspace_prompt_section(workspace, mode)
    if mode == "full":
        from app.agent.prompt import full_access_prompt_section
        system_prompt += full_access_prompt_section(workspace=workspace)
    permissions = []
    if cfg.permissions_allow_read:
        permissions.append(
            FilesystemPermission(
                operations=["read"],
                paths=[_to_route_path(p) for p in cfg.permissions_allow_read],
                mode="allow",
            )
        )
    if cfg.permissions_allow_write:
        permissions.append(
            FilesystemPermission(
                operations=["write"],
                paths=[_to_route_path(p) for p in cfg.permissions_allow_write],
                mode="allow",
            )
        )

    task_mw = TaskStateMiddleware()
    context_middleware = []
    if context_model is not None and backend is not None:
        from app.agent.context import build_summary_middleware
        context_middleware = [build_summary_middleware(context_model, backend=backend, preferences=preferences,
            session_id=f"{session_id}:subagent:{cfg.name}", request_projectors=[task_mw])]
    spec = SubAgent(
        name=cfg.name,
        description=cfg.description,
        system_prompt=system_prompt,
        permissions=permissions or None,
        # 只剥离 `_intent`，不注入 schema。
        # 子智能体的工具调用走 `subagent_tool` 事件（不在这期的契约里），所以
        # 不需要它们产标签；但**剥离必须生效** —— 模型见过 `_intent` 这个字段后
        # 完全可能自作主张带上它，工具收到意外关键字参数会直接报错，那就破坏了
        # "展示层增强不许影响执行"这条底线。
        #
        # 护栏必须**单独挂**（子智能体不继承主 Agent 的中间件栈）。少了这一行，
        # 子智能体照样能跑 `Stop-Process -Name python -Force` 把后端杀掉 ——
        # 而"调用者是谁"对后果毫无影响：死的是同一个进程。
        middleware=[build_intent_strip_middleware(), ClarificationMiddleware(), build_self_kill_guard_middleware(), task_mw]
        + context_middleware + _host_filesystem_middleware(backend, permissions, mode),
    )
    if not permissions:
        # Omitting the key inherits parent permissions; explicit None disables them.
        spec.pop("permissions", None)
    if inherited_tools is not None:
        # Durable shared-memory writes belong to the main agent; a read-only
        # research delegate must not gain a filesystem-permission bypass.
        spec["tools"] = [tool for tool in inherited_tools if getattr(tool, "name", None) != "remember"]
    return spec


def _host_filesystem_middleware(backend, permissions, mode):
    if mode != "full" or backend is None:
        return []
    from app.agent.host_shell import host_filesystem_middleware
    return host_filesystem_middleware(backend, permissions)


def load_all_subagents(*, workspace: Path | None = None, mode: str = "sandbox", backend=None,
                       context_model=None, preferences=None, session_id=None, inherited_tools=None) -> list:
    """加载所有子智能体，返回 deepagents SubAgent 对象列表（供 create_deep_agent(subagents=...))."""
    configs = load_all_subagents_cfg()
    if context_model is not None:
        from deepagents.middleware.subagents import GENERAL_PURPOSE_SUBAGENT
        if not any(c.name == GENERAL_PURPOSE_SUBAGENT["name"] for c in configs):
            configs.append(SubAgentConfig(name=GENERAL_PURPOSE_SUBAGENT["name"],
                description=GENERAL_PURPOSE_SUBAGENT["description"], system_prompt=GENERAL_PURPOSE_SUBAGENT["system_prompt"]))
    return [build_subagent(c, workspace=workspace, mode=mode, backend=backend, context_model=context_model,
                          preferences=preferences, session_id=session_id, inherited_tools=inherited_tools) for c in configs]


@lru_cache
def available_subagents() -> list[str]:
    """返回子智能体名字列表（辅助/调试）。"""
    return [c.name for c in load_all_subagents_cfg()]


def reload_subagent_cache() -> None:
    """使 available_subagents 缓存失效（改配置后调用）。"""
    available_subagents.cache_clear()


__all__ = [
    "WORKSPACE_PREFIX",
    "SubAgentConfig",
    "available_subagents",
    "build_subagent",
    "load_all_subagents",
    "load_all_subagents_cfg",
    "reload_subagent_cache",
]
