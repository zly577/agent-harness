"""P8 审批模块单元测试（不依赖真实模型/网络）。"""
import pytest

from app.agent.approvals import build_decision


def test_build_decision_approve():
    actions = [{"type": "tool_call"}, {"type": "tool_call"}]
    dec = build_decision(actions, approve=True)
    assert dec == [{"type": "approve"}, {"type": "approve"}]


def test_build_decision_reject():
    actions = [{"type": "tool_call"}]
    dec = build_decision(actions, approve=False)
    assert dec == [{"type": "reject"}]


def test_build_decision_no_actions_defaults_one():
    # 无 actions 时默认构造一个决策（用于单待审批项）
    dec = build_decision(None, approve=True)
    assert dec == [{"type": "approve"}]


def test_default_permissions_have_interrupt():
    """默认权限包含 interrupt 模式（触发审批）。"""
    from app.agent.sandbox import default_permissions
    perms = default_permissions()
    assert perms, "应至少有一条权限"
    assert all(p.mode == "interrupt" for p in perms)
    assert any("/system" in p.paths[0] for p in perms)


def test_permission_matches_sensitive_path():
    """写入 /system/** 下的文件应判定为 interrupt。"""
    from app.agent.sandbox import default_permissions
    from deepagents.middleware.filesystem import _check_fs_permission
    perms = default_permissions()
    assert _check_fs_permission(perms, "write", "/system/config.txt") == "interrupt"
    # 非敏感路径应放行（allow）
    assert _check_fs_permission(perms, "write", "/notes/x.txt") == "allow"
