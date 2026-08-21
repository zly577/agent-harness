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


def test_available_subagents_contains_example():
    names = subagents.available_subagents()
    assert "researcher" in names
