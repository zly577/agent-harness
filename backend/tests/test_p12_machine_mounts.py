"""P12 测试：默认环境 = 本机电脑，只有 execute 进沙箱。

覆盖：
1. `HARNESS_MACHINE_ACCESS` 关闭时行为与 P11 完全一致（只有 `/workspace`）；
2. 打开后 `/mnt/<名>/...` 直接读写**本机真实文件**（不经过沙箱、不需要沙箱在线）；
3. `/mnt` 这一层的 `ls` 只列挂载点，不会漏到工作区；误用路径给可读错误；
4. 越界（`..`）仍被拒 —— 挂载点内部仍有 virtual_mode 边界；
5. 权限规则（写操作需审批）覆盖挂载点，且仍满足 deepagents 的 route 前缀校验；
6. 子智能体权限路径识别 `/mnt/...` 前缀；
7. execute 仍然只进沙箱（有挂载点时不受影响）；
8. 方案 A：把挂载点只读挂进容器（容器内路径 = `/mnt/<名>`）、配置指纹进沙箱缓存签名、
   配置变更强制重建容器、降级路径不影响同步范围。

假容器复用 `test_p11_hybrid_backend` 的实现（不联网、不建真容器）。
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

import pytest

from app.agent import opensandbox_backend as osb
from app.agent import sandbox
from app.agent import subagents as sub
from app.core.config import settings
from tests.test_p11_hybrid_backend import CONTAINER_ROOT, _FakeClient


@pytest.fixture()
def tmp_path(request):
    """覆写 pytest 的 tmp_path：用工作区内目录，避免受限沙箱下 rmtree 失败（同 P11）。"""
    safe = re.sub(r"[^A-Za-z0-9_.-]+", "_", request.node.name)[:60]
    base = Path(__file__).resolve().parent / "_tmp_hybrid" / f"p12_{safe}"
    base.mkdir(parents=True, exist_ok=True)
    return base


@pytest.fixture()
def workspace(tmp_path):
    ws = tmp_path / "ws"
    ws.mkdir(parents=True, exist_ok=True)
    return ws


@pytest.fixture()
def host(tmp_path):
    """扮演「用户电脑上的某个目录」，挂载成 /mnt/tmp。"""
    h = tmp_path / "host_drive"
    (h / "proj").mkdir(parents=True, exist_ok=True)
    (h / "proj" / "existing.py").write_text("print('hi')\n", encoding="utf-8")
    return h


@pytest.fixture()
def machine(monkeypatch, host):
    """打开本机磁盘访问，把 host 目录挂成 /mnt/tmp。"""
    monkeypatch.setattr(settings, "harness_machine_access", True)
    monkeypatch.setattr(settings, "harness_machine_mounts", f"tmp={host.as_posix()}")
    return host


@pytest.fixture()
def no_machine(monkeypatch):
    monkeypatch.setattr(settings, "harness_machine_access", False)
    monkeypatch.setattr(settings, "harness_machine_mounts", "")


@pytest.fixture()
def fake_sandbox(tmp_path, monkeypatch):
    """把 registry.get 换成假容器（同 P11 的假容器实现）。"""
    host_dir = tmp_path / "container"
    host_dir.mkdir(parents=True, exist_ok=True)
    client = _FakeClient(host_dir)

    monkeypatch.setattr(settings, "sandbox_enabled", True)
    monkeypatch.setattr(settings, "sandbox_sync_enabled", True)
    monkeypatch.setattr(settings, "sandbox_root", CONTAINER_ROOT)

    def fake_get(session_key: str, path_prefix=None):
        return osb._SandboxEntry(
            client=client,
            connection_key=session_key,
            mapper=osb.PathMapper(CONTAINER_ROOT, prefix=path_prefix),
        )

    monkeypatch.setattr(osb.registry, "get", fake_get)
    return client


# --------------------------------------------------------------------------
# 1. 默认关闭时行为不变
# --------------------------------------------------------------------------


def test_machine_access_off_by_default(workspace, no_machine):
    """关掉开关 → 没有 /mnt 路由，挂载点为空，也没有任何需要审批的路径（P11/P21 行为）。"""
    assert osb.machine_mounts() == []
    be = sandbox.build_main_backend(session_key="t", workspace=str(workspace))
    assert set(be.routes) == {sandbox.PERMISSION_ROUTE_PREFIX}
    # P21：本机访问关闭 ⇒ 没有本机路径可审批 ⇒ 空列表。
    # 工作区 /workspace/** 不再列入审批（它是"沙箱内的地盘"，读写真自由）。
    assert sandbox.default_permissions() == []


def test_machine_mounts_auto_detects_drives(monkeypatch):
    """留空挂载配置 → 自动挂载本机存在的盘符（Windows）。"""
    monkeypatch.setattr(settings, "harness_machine_access", True)
    monkeypatch.setattr(settings, "harness_machine_mounts", "")
    mounts = osb.machine_mounts()
    if sys.platform.startswith("win"):
        assert mounts, "Windows 上至少应自动挂载 C:/"
        assert all(name.isalnum() for name, _ in mounts)
        assert all(p.is_dir() for _, p in mounts)
    else:
        assert mounts == []  # 非 Windows 没有盘符概念


# --------------------------------------------------------------------------
# 2. /mnt/<名>/... 直读直写本机真实文件
# --------------------------------------------------------------------------


def test_machine_routes_wired(workspace, machine):
    """/mnt/tmp/ 与 /mnt/ 两条 route 存在，default 仍是可执行的混合后端。"""
    from deepagents.middleware.filesystem import supports_execution

    be = sandbox.build_main_backend(session_key="t", workspace=str(workspace))
    assert "/mnt/tmp/" in be.routes
    assert "/mnt/" in be.routes
    assert sandbox.PERMISSION_ROUTE_PREFIX in be.routes
    assert be.routes[sandbox.PERMISSION_ROUTE_PREFIX] is be.default
    # 挂载点是独立后端，没抢走 default；execute 依旧走沙箱
    assert isinstance(be.default, osb.HybridDefaultBackend)
    assert supports_execution(be) is True


def test_file_tools_write_real_host_files(workspace, machine):
    """write/read/edit 落在**本机真实文件**上，而不是工作区、也不是沙箱。"""
    be = sandbox.build_main_backend(session_key="t", workspace=str(workspace))

    res = be.write("/mnt/tmp/proj/gen.py", "print(1)\n")
    assert res.error is None
    assert (machine / "proj" / "gen.py").read_text(encoding="utf-8") == "print(1)\n"

    assert be.read("/mnt/tmp/proj/gen.py").file_data["content"] == "print(1)\n"
    assert be.read("/mnt/tmp/proj/existing.py").file_data["content"] == "print('hi')\n"

    edit = be.edit("/mnt/tmp/proj/gen.py", "print(1)", "print(2)")
    assert edit.error is None
    assert (machine / "proj" / "gen.py").read_text(encoding="utf-8") == "print(2)\n"

    # 没有多写一份到工作区，也没有多出 "mnt/tmp" 目录层级
    assert not (workspace / "mnt").exists()
    assert not (workspace / "proj").exists()


def test_mount_read_write_works_without_sandbox(workspace, machine, monkeypatch):
    """本机读写不依赖沙箱：沙箱关掉也照常（只有 execute 报未启用）。"""
    monkeypatch.setattr(settings, "sandbox_enabled", False)
    be = sandbox.build_main_backend(session_key="t", workspace=str(workspace))

    assert be.write("/mnt/tmp/note.txt", "local").error is None
    assert (machine / "note.txt").read_text(encoding="utf-8") == "local"
    assert be.execute("echo hi").exit_code == 1


def test_workspace_and_mount_are_parallel_worlds(workspace, machine, fake_sandbox):
    """同一个 backend 上：/workspace 落工作区，/mnt 落本机 —— 互不影响。"""
    be = sandbox.build_main_backend(session_key="t", workspace=str(workspace))
    be.write("/workspace/a.txt", "A")
    be.write("/mnt/tmp/b.txt", "B")

    assert (workspace / "a.txt").read_text(encoding="utf-8") == "A"
    assert (machine / "b.txt").read_text(encoding="utf-8") == "B"
    assert not (machine / "a.txt").exists()
    assert not (workspace / "b.txt").exists()
    # 文件工具全程零沙箱调用
    assert fake_sandbox.files.calls == []


def test_ls_and_glob_on_mounts(workspace, machine):
    """ls/glob 的路径同样是模型视角的 /mnt/... 前缀。"""
    be = sandbox.build_main_backend(session_key="t", workspace=str(workspace))

    assert [e["path"] for e in be.ls("/mnt").entries] == ["/mnt/tmp"]
    assert [e["path"] for e in be.ls("/mnt/tmp/proj").entries] == ["/mnt/tmp/proj/existing.py"]

    matches = [m["path"] for m in be.glob("/mnt/tmp/proj/*.py", "/mnt/tmp/proj").matches]
    assert matches == ["/mnt/tmp/proj/existing.py"]
    # path=None 时 CompositeBackend 会搜所有后端，模式里的前缀同样要能识别
    assert "/mnt/tmp/proj/existing.py" in [
        m["path"] for m in be.glob("/mnt/tmp/**/*.py").matches
    ]

    root_entries = {e["path"] for e in be.ls("/").entries}
    assert {"/mnt/tmp/", "/mnt/", "/workspace/"} <= root_entries


def test_mount_escape_is_rejected(workspace, machine):
    """挂载点内部仍关在盘符/目录里：`..` 越界被拒（virtual_mode）。

    注意行为是**抛 ValueError**（FilesystemBackend 的安全检查），不是返回 error 结果 ——
    与工作区 `/workspace/../x` 的表现一致；deepagents 的工具层会把它转成工具错误。
    """
    be = sandbox.build_main_backend(session_key="t", workspace=str(workspace))
    with pytest.raises(ValueError):
        be.write("/mnt/tmp/../escape.txt", "nope")
    assert not (machine.parent / "escape.txt").exists()


def test_misuse_of_mount_root_is_readable(workspace, machine):
    """`/mnt/nope/...`（不存在的挂载点）给可读错误，而不是静默写到别处。"""
    be = sandbox.build_main_backend(session_key="t", workspace=str(workspace))
    res = be.write("/mnt/nope/x.txt", "y")
    assert res.error and "挂载" in res.error
    assert be.read("/mnt/nope/x.txt").error


def test_workspace_glob_with_double_absolute_path(workspace):
    """P12 顺带修的坑：`glob` 的模式是**相对 search path** 解释的。

    P11 下 `glob('/workspace/sub/*.txt', '/workspace/sub')` 永远是空结果（绝对模式被拿去
    和相对路径比对），挂载点上同理。归一化后两者都能匹配。
    """
    be = sandbox.build_main_backend(session_key="t", workspace=str(workspace))
    sub = workspace / "sub"
    sub.mkdir(exist_ok=True)
    (sub / "deep.txt").write_text("deep", encoding="utf-8")

    assert [m["path"] for m in be.glob("/workspace/sub/*.txt", "/workspace/sub").matches] == [
        "/workspace/sub/deep.txt"
    ]
    # path=None → CompositeBackend 会"搜所有后端"；`/workspace/` 这条占位 route 会让同一个
    # 文件既以"不带前缀"（default 的原始结果）又以"带前缀"（route remap）出现 —— P11 遗留的
    # 别名副作用（见 docs/P12 §已知边界），这里只要求带前缀的那条在。
    assert "/workspace/sub/deep.txt" in [m["path"] for m in be.glob("/workspace/sub/*.txt").matches]


# --------------------------------------------------------------------------
# 3. 权限与子智能体 route 前缀
# --------------------------------------------------------------------------


def test_permissions_cover_mounts_and_pass_route_check(workspace, machine):
    """访问挂载点要人工审批（**读和写都要**），且权限路径仍落在 route 前缀内。

    P21 起审批边界是"只有碰本机才问"：
    - 工作区 `/workspace/**` 不再列入（沙箱内自由读写）；
    - 本机 `/mnt/<名>/**` 的 read + write 都触发审批。
    """
    from deepagents.middleware.filesystem import _all_paths_scoped_to_routes

    be = sandbox.build_main_backend(session_key="t", workspace=str(workspace))
    perms = sandbox.default_permissions()
    paths = [p for rule in perms for p in rule.paths]
    assert paths == ["/mnt/tmp/**"], "只审批本机挂载点，工作区不审批"
    assert "/workspace/**" not in paths
    assert all(rule.mode == "interrupt" for rule in perms)
    assert all(rule.operations == ["read", "write"] for rule in perms)
    assert _all_paths_scoped_to_routes(perms, be) is True
    assert sandbox.validate_permissions(be) == []


def test_subagent_permission_paths_recognise_mounts(workspace, machine):
    """子智能体 YAML 里可以直接写 /mnt/... 路径。"""
    assert sub._to_route_path("/") == "/workspace/**"
    assert sub._to_route_path("/notes/") == "/workspace/notes/**"
    assert sub._to_route_path("/workspace/a") == "/workspace/a/**"
    assert sub._to_route_path("/mnt/tmp/proj") == "/mnt/tmp/proj/**"
    assert sub._to_route_path("/mnt/tmp") == "/mnt/tmp/**"


def test_subagent_mount_path_falls_back_to_workspace_when_disabled(workspace, no_machine):
    """没开本机访问时，/mnt/... 会被当成工作区相对路径（不会产生越界 route 错误）。"""
    assert sub._to_route_path("/mnt/d/proj") == "/workspace/mnt/d/proj/**"


# --------------------------------------------------------------------------
# 4. execute 仍然只进沙箱
# --------------------------------------------------------------------------


def test_execute_still_targets_sandbox_only(workspace, machine, fake_sandbox):
    """有挂载点时 execute 依旧在容器里跑，且同步只覆盖 /workspace。"""
    be = sandbox.build_main_backend(session_key="t", workspace=str(workspace))
    be.write("/workspace/data.txt", "42")
    be.write("/mnt/tmp/only_local.txt", "LOCAL")

    out = be.execute("python -c \"print(open('/workspace/data.txt').read())\"")
    assert out.exit_code == 0
    assert "42" in out.output
    # 本机挂载点上的文件**不会**被推进容器
    assert not (fake_sandbox.host_root / "only_local.txt").exists()
    assert not (fake_sandbox.host_root / "mnt").exists()


def test_sandbox_status_reports_mounts(machine):
    """状态摘要（/health、前端调试用）里能看到挂载点。"""
    status = osb.sandbox_status()
    assert status["machine_access"] is True
    assert [m["virtual"] for m in status["machine_mounts"]] == ["/mnt/tmp/"]
    assert status["machine_mounts"][0]["path"] == str(machine)


# --------------------------------------------------------------------------
# 5. 方案 A：把挂载点只读挂进沙箱容器
# --------------------------------------------------------------------------


@pytest.fixture()
def mount_into_sandbox(monkeypatch, machine):
    """在 machine 的基础上打开「挂载进沙箱」。"""
    monkeypatch.setattr(settings, "sandbox_mount_machine_dirs", True)
    monkeypatch.setattr(settings, "sandbox_mount_read_only", True)
    return machine


def test_sandbox_mount_specs_off_by_default(machine):
    """默认不挂进容器 —— 容器里没有 /mnt（P12 原行为，不能悄悄改变）。"""
    assert settings.sandbox_mount_machine_dirs is False
    assert osb.sandbox_mount_specs() == []
    assert osb.sandbox_mount_key() == ""
    assert osb._build_sandbox_volumes() == []


def test_sandbox_mount_specs_reuse_mnt_paths(monkeypatch, mount_into_sandbox):
    """容器内路径**沿用模型侧** `/mnt/<名>` —— 这是整个方案的关键。"""
    specs = osb.sandbox_mount_specs()
    assert specs == [(mount_into_sandbox.as_posix(), "/mnt/tmp")]
    # 与 machine_mount_prefix 对齐（去掉尾斜杠）
    assert specs[0][1] == osb.machine_mount_prefix("tmp").rstrip("/")


def test_sandbox_mount_specs_empty_when_machine_access_off(monkeypatch, machine):
    """本机访问关掉 → 没有挂载点 → 即便开了挂载开关也没有东西可挂。"""
    monkeypatch.setattr(settings, "sandbox_mount_machine_dirs", True)
    monkeypatch.setattr(settings, "harness_machine_access", False)
    assert osb.sandbox_mount_specs() == []


def test_mount_key_reflects_config(mount_into_sandbox, monkeypatch):
    """指纹必须随「挂载点」与「读写模式」变化，否则会复用错误的容器。"""
    key_ro = osb.sandbox_mount_key()
    assert key_ro.startswith("ro=True;")
    assert "/mnt/tmp" in key_ro

    monkeypatch.setattr(settings, "sandbox_mount_read_only", False)
    key_rw = osb.sandbox_mount_key()
    assert key_rw != key_ro and key_rw.startswith("ro=False;")

    # 再改回只读 → 指纹回到原值（幂等，不会无谓重建）
    monkeypatch.setattr(settings, "sandbox_mount_read_only", True)
    assert osb.sandbox_mount_key() == key_ro


def test_build_sandbox_volumes_maps_to_sdk(mount_into_sandbox):
    """规格 → SDK Volume 的字段映射（name / host.path / mount_path / read_only）。"""
    pytest.importorskip("opensandbox")
    vols = osb._build_sandbox_volumes()
    assert len(vols) == 1
    v = vols[0]
    assert v.name == "machine-tmp"
    assert v.mount_path == "/mnt/tmp"
    assert v.read_only is True
    assert v.host is not None
    assert v.host.path == mount_into_sandbox.as_posix()


def test_mount_fallback_skips_unrelated_errors():
    """网络/超时/限流与挂载无关 → 不该退回无挂载重试（那会白等一轮）。

    实测触发场景：OpenSandbox 服务没起时抛 `SandboxConnectionException`，
    若不加判断就会白跑第二次创建（创建本身还有 4 次内部重试 + 退避）。
    """

    class SandboxConnectionException(Exception):
        pass

    class SandboxTimeoutException(Exception):
        pass

    class SandboxRateLimitException(Exception):
        pass

    class SandboxApiException(Exception):
        pass

    class SomethingElse(Exception):
        pass

    assert osb._mount_fallback_worthwhile(SandboxConnectionException("refused")) is False
    assert osb._mount_fallback_worthwhile(SandboxTimeoutException("timeout")) is False
    assert osb._mount_fallback_worthwhile(SandboxRateLimitException("429")) is False
    # 服务端明确拒绝（最可能的原因就是运行时拒绝 host mount）→ 值得降级重试
    assert osb._mount_fallback_worthwhile(SandboxApiException("hostPath not allowed")) is True
    # 未知异常宁可多试一次，也不要把沙箱彻底搞挂
    assert osb._mount_fallback_worthwhile(SomethingElse("boom")) is True


def test_registry_rebuilds_container_when_mount_config_changes(mount_into_sandbox, monkeypatch):
    """volumes 是**创建期**参数：配置一变必须换新容器，否则是「配置改了没反应」。

    不需要真容器：把 `_create` 换成记录调用的假实现。
    """
    reg = osb.SandboxRegistry()

    class _FakeClient:
        def is_healthy(self) -> bool:
            return True

        def destroy(self) -> None:  # pragma: no cover - atexit 兜底
            pass

    created: list[str] = []

    def fake_create(key: str, path_prefix=None):
        created.append(osb.sandbox_mount_key())
        return osb._SandboxEntry(
            client=_FakeClient(),
            connection_key=key,
            mapper=osb.PathMapper("/workspace"),
            volumes_key=osb.sandbox_mount_key(),
        )

    monkeypatch.setattr(reg, "_create", fake_create)

    first = reg.get("s1")
    assert len(created) == 1
    # 配置未变 + 容器健康 → 复用，不重建
    assert reg.get("s1") is first
    assert len(created) == 1

    # 读写模式变了 → 必须重建（旧容器是只读挂载的）
    monkeypatch.setattr(settings, "sandbox_mount_read_only", False)
    second = reg.get("s1")
    assert second is not first
    assert len(created) == 2

    # 挂载开关关掉（指纹变空）→ 同样必须重建
    monkeypatch.setattr(settings, "sandbox_mount_machine_dirs", False)
    third = reg.get("s1")
    assert third is not second
    assert len(created) == 3
    assert created[-1] == ""


def test_sandbox_status_exposes_volumes(mount_into_sandbox):
    """状态摘要里能看到「挂载进沙箱」的开关与规格（排查挂载没生效时最有用）。"""
    status = osb.sandbox_status()
    assert status["mount_into_sandbox"] is True
    assert status["mount_read_only"] is True
    assert status["sandbox_volumes"] == [
        {"host": mount_into_sandbox.as_posix(), "container": "/mnt/tmp"}
    ]


def test_sandbox_status_volumes_empty_by_default(machine):
    status = osb.sandbox_status()
    assert status["mount_into_sandbox"] is False
    assert status["sandbox_volumes"] == []


def test_execute_still_syncs_only_workspace_with_mounts_enabled(
    workspace, mount_into_sandbox, fake_sandbox
):
    """即便挂载点进了容器，同步逻辑仍**只**覆盖 /workspace（挂载点是只读镜像，不该被推）。"""
    be = sandbox.build_main_backend(session_key="t", workspace=str(workspace))
    be.write("/workspace/data.txt", "42")
    be.write("/mnt/tmp/only_local.txt", "LOCAL")

    out = be.execute("python -c \"print(open('/workspace/data.txt').read())\"")
    assert out.exit_code == 0
    assert "42" in out.output
    assert not (fake_sandbox.host_root / "only_local.txt").exists()
    assert not (fake_sandbox.host_root / "mnt").exists()
