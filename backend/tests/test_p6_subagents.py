"""P6 测试：子智能体 YAML 注册表 + SubAgent 构建（不触发真实模型）。

注意：deepagents 的 SubAgent 是 TypedDict（dict），故用下标访问。
"""
import pytest

from app.agent import subagents
from app.agent.subagents import SubAgentConfig


def test_example_subagent_config_valid():
    cfgs = subagents.load_all_subagents_cfg()
    names = [c.name for c in cfgs]
    assert "researcher" in names


def test_invalid_name_rejected():
    with pytest.raises(ValueError):
        SubAgentConfig(name="bad name!", description="x", system_prompt="y")


def test_empty_prompt_rejected():
    with pytest.raises(ValueError):
        SubAgentConfig(name="ok-name", description="x", system_prompt="   ")


def test_build_subagent_omits_model():
    """build_subagent 生成 dict，不含 model（继承主 agent）。"""
    cfg = SubAgentConfig(name="researcher", description="d", system_prompt="prompt")
    sa = subagents.build_subagent(cfg)
    assert isinstance(sa, dict)
    assert sa["name"] == "researcher"
    assert sa.get("model") is None


def test_build_subagent_permissions():
    cfg = SubAgentConfig(
        name="writer", description="d", system_prompt="prompt",
        permissions_allow_write=["/notes/"],
    )
    sa = subagents.build_subagent(cfg)
    perms = sa.get("permissions")
    assert perms is not None
    ops = {tuple(sorted(p.operations)) for p in perms}
    assert ("write",) in ops


def test_subagent_permission_paths_are_route_scoped():
    """P11 回归：子智能体权限路径必须落在 /workspace route 前缀内。

    背景：deepagents 为每个子智能体单独建 FilesystemMiddleware，用的是父级（可执行）
    backend，因此子智能体的权限路径也受「必须落在 route 前缀内」约束；否则
    `create_deep_agent` 直接抛 NotImplementedError（且只在有子智能体时才暴露）。
    """
    from app.agent.sandbox import PERMISSION_ROUTE_PREFIX, WORKSPACE_PREFIX

    for cfg in subagents.load_all_subagents_cfg():
        sa = subagents.build_subagent(cfg)
        for rule in sa.get("permissions") or []:
            for path in rule.paths:
                assert path.startswith(PERMISSION_ROUTE_PREFIX), (
                    f"{cfg.name} 的权限路径 {path} 不在 route 前缀 {PERMISSION_ROUTE_PREFIX} 内"
                )
                assert path.startswith(WORKSPACE_PREFIX)
                assert path.endswith("**"), f"{path} 缺通配，目录结尾不匹配子孙路径"


def test_route_path_normalization():
    """`/` → 整个工作区；`/notes/` → 工作区子目录；已带前缀的不重复叠加。"""
    from app.agent.sandbox import WORKSPACE_PREFIX

    assert subagents._to_route_path("/") == f"{WORKSPACE_PREFIX}/**"
    assert subagents._to_route_path("/notes/") == f"{WORKSPACE_PREFIX}/notes/**"
    assert subagents._to_route_path("/notes") == f"{WORKSPACE_PREFIX}/notes/**"
    assert subagents._to_route_path(f"{WORKSPACE_PREFIX}/a/b") == f"{WORKSPACE_PREFIX}/a/b/**"
    assert subagents._to_route_path(f"{WORKSPACE_PREFIX}/a/**") == f"{WORKSPACE_PREFIX}/a/**"


def test_validate_permissions_passes_for_real_config():
    """主 Agent + 全部子智能体的权限都应通过启动期自检。"""
    from app.agent.sandbox import build_main_backend, validate_permissions

    be = build_main_backend(session_key="t")
    assert validate_permissions(be) == []


def test_available_subagents_contains_example():
    names = subagents.available_subagents()
    assert "researcher" in names
