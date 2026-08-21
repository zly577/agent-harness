"""子智能体注册表（P6）。

从 `configs/subagents/*.yaml` 加载子智能体定义，经 Pydantic 校验后构建 deepagents `SubAgent`。

关键：**子智能体不传 model → 继承主 Agent 的模型**（官方确认），因此这里用同一个 DeepSeek。
隔离：deepagents 的 `task` 工具让子任务运行在 **isolated context window**（上下文隔离）；
工具/权限隔离通过 `permissions` + 不开放 execute 实现。
"""
from functools import lru_cache
from pathlib import Path

import yaml
from pydantic import BaseModel, Field, field_validator

_CONFIG_DIR = Path(__file__).resolve().parent.parent.parent / "configs" / "subagents"


class SubAgentConfig(BaseModel):
    """一个 YAML 子智能体定义。"""

    name: str
    description: str
    system_prompt: str
    # 可选：指定工具。为空则继承主 Agent 的默认工具（文件工具等）。
    tools: list[str] = Field(default_factory=list, description="工具名白名单，空=继承默认")
    # 可选：文件权限（objects -> allow/deny/interrupt）
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


def build_subagent(cfg: SubAgentConfig):
    """把配置构建为 deepagents SubAgent（省略 model 以继承主 Agent 模型）。"""
    from deepagents import SubAgent
    from deepagents.middleware.filesystem import FilesystemPermission

    permissions = []
    if cfg.permissions_allow_read:
        permissions.append(
            FilesystemPermission(operations=["read"], paths=cfg.permissions_allow_read, mode="allow")
        )
    if cfg.permissions_allow_write:
        permissions.append(
            FilesystemPermission(operations=["write"], paths=cfg.permissions_allow_write, mode="allow")
        )

    return SubAgent(
        name=cfg.name,
        description=cfg.description,
        system_prompt=cfg.system_prompt,
        permissions=permissions or None,
    )


def load_all_subagents() -> list:
    """加载所有子智能体，返回 deepagents SubAgent 对象列表（供 create_deep_agent(subagents=...))."""
    return [build_subagent(c) for c in load_all_subagents_cfg()]


@lru_cache
def available_subagents() -> list[str]:
    """返回子智能体名字列表（辅助/调试）。"""
    return [c.name for c in load_all_subagents_cfg()]


def reload_subagent_cache() -> None:
    """使 available_subagents 缓存失效（改配置后调用）。"""
    available_subagents.cache_clear()
