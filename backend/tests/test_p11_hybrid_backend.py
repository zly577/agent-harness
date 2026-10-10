"""测试：本地磁盘读写 + OpenSandbox 执行的判断路由（不联网、不建真容器）。

覆盖：
1. backend 组合形态（CompositeBackend + 混合 default）与 deepagents 的 execute 注册条件；
2. 文件工具 → **本地磁盘**（零远程往返；沙箱不开也照常工作）；
3. execute → 沙箱（假容器）：命令翻译、工作目录、执行前推本地改动、执行后拉回产出；
4. 路径前缀 `/workspace` 折算、权限 route 校验。

假容器说明（Windows 上跑不了真 Linux 容器）：
- `files` 服务把容器路径 `/workspace/x` 折成宿主目录 `<root>/x`，模拟"容器里读到了同一份文件"；
- `commands.run` 记录命令，并把 `find`（内部清单）与 `open(...)`（读写）做等价模拟。
"""
from __future__ import annotations

import asyncio
import re
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace

import pytest

from app.agent import opensandbox_backend as osb
from app.agent import sandbox
from app.core.config import settings

CONTAINER_ROOT = "/workspace"


# --------------------------------------------------------------------------
# 假容器
# --------------------------------------------------------------------------


class _FakeFiles:
    """容器 files 服务：容器路径 `/workspace/x` <-> 宿主 `<root>/x`。"""

    def __init__(self, host_root: Path) -> None:
        self.host_root = host_root
        self.calls: list[tuple[str, str]] = []

    @staticmethod
    def _rel(container_path: str) -> str:
        rel = str(container_path)
        if rel.startswith(CONTAINER_ROOT):
            rel = rel[len(CONTAINER_ROOT):]
        return rel.lstrip("/")

    def _host(self, container_path: str) -> Path:
        return self.host_root / self._rel(container_path)

    def write_file(self, path, data, **_kw):
        self.calls.append(("write_file", str(path)))
        target = self._host(str(path))
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(data if isinstance(data, bytes) else str(data).encode())

    def read_bytes(self, path, **_kw):
        self.calls.append(("read_bytes", str(path)))
        return self._host(str(path)).read_bytes()

    def list_directory(self, entry, **_kw):
        base = self._host(str(entry.path))
        out = []
        if base.is_dir():
            for child in sorted(base.iterdir()):
                st = child.stat()
                out.append(
                    SimpleNamespace(
                        path=f"{CONTAINER_ROOT}/{child.relative_to(self.host_root).as_posix()}",
                        entry_type="directory" if child.is_dir() else "file",
                        size=st.st_size if child.is_file() else 0,
                        modified_at=datetime.fromtimestamp(st.st_mtime),
                    )
                )
        return out


class _FakeCommands:
    """容器 commands 服务：内部清单命令真列目录，其余命令解析 open(...) 做等价读写。"""

    def __init__(self, files: _FakeFiles) -> None:
        self.files = files
        self.runs: list[tuple[str, str | None]] = []

    def _find(self) -> str:
        root = self.files.host_root
        lines = []
        if root.is_dir():
            for p in sorted(root.rglob("*")):
                if p.is_file():
                    st = p.stat()
                    lines.append(f"{st.st_mtime}\t{st.st_size}\t{CONTAINER_ROOT}/{p.relative_to(root).as_posix()}")
        return "\n".join(lines)

    def run(self, command: str, opts=None, **_kw):
        cwd = getattr(opts, "working_directory", None) if opts is not None else None
        self.runs.append((command, cwd))
        if command.startswith("find "):
            out = self._find()
            return SimpleNamespace(
                exit_code=0,
                logs=SimpleNamespace(stdout=[SimpleNamespace(text=out)] if out else [], stderr=[]),
                result=[],
            )
        out = ""
        for path, mode, value in re.findall(
            r"open\('([^']+)'(?:,'(\w)')?\)(?:\.write\(str\(([^)]+)\)\))?", command
        ):
            if mode == "w":
                text = str(eval(value))  # noqa: S307 - 仅测试用，表达式来自本测试
                self.files.write_file(path, text)
                out += text
            else:
                out += self.files.read_bytes(path).decode()
        return SimpleNamespace(
            exit_code=0,
            logs=SimpleNamespace(stdout=[SimpleNamespace(text=out)] if out else [], stderr=[]),
            result=[],
        )


class _FakeClient:
    def __init__(self, host_root: Path) -> None:
        self.host_root = host_root
        self.files = _FakeFiles(host_root)
        self.commands = _FakeCommands(self.files)
        self.id = "fake-sandbox"

    def is_healthy(self) -> bool:
        return True

    def destroy(self) -> None:
        pass


@pytest.fixture()
def fake_sandbox(tmp_path, monkeypatch):
    """把 registry.get 换成假容器（容器根 = tmp_path/container）。"""
    host = tmp_path / "container"
    host.mkdir(parents=True, exist_ok=True)
    client = _FakeClient(host)

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


@pytest.fixture()
def tmp_path(request):
    """覆写 pytest 的 tmp_path：用「无需清理」的目录，避免删不掉导致 setup 报错。

    原因：pytest 默认的 tmp_path 在 `%TEMP%` 下，并由 pytest 负责 rmtree。在受限沙箱
    （workspace-write）里栈外目录不可写/不可删，rmtree 会抛 PermissionError，把测试
    卡在 setup 阶段。本 fixture 直接在工作区里开目录，不注册清理（内容物很小）。
    """
    pytest.importorskip("app")
    safe = re.sub(r"[^A-Za-z0-9_.-]+", "_", request.node.name)[:60]
    base = Path(__file__).resolve().parent / "_tmp_hybrid" / safe
    base.mkdir(parents=True, exist_ok=True)
    return base


def _writable(p: Path) -> bool:
    try:
        probe = p / ".probe"
        probe.write_text("x", encoding="utf-8")
        probe.unlink()
        return True
    except OSError:
        return False


@pytest.fixture()
def workspace(tmp_path):
    ws = tmp_path / "ws"
    ws.mkdir(parents=True, exist_ok=True)
    return ws


# --------------------------------------------------------------------------
# 1. 组合形态与门禁
# --------------------------------------------------------------------------


def test_execute_allowed_follows_sandbox_enabled(monkeypatch):
    monkeypatch.setattr(settings, "sandbox_enabled", True)
    assert sandbox.execute_allowed() is True
    monkeypatch.setattr(settings, "sandbox_enabled", False)
    assert sandbox.execute_allowed() is False


def test_main_backend_shape(workspace):
    """默认 backend = CompositeBackend(default=混合后端, routes={/workspace/})。"""
    from deepagents.backends import CompositeBackend
    from deepagents.middleware.filesystem import supports_execution

    be = sandbox.build_main_backend(session_key="t", workspace=str(workspace))
    assert isinstance(be, CompositeBackend)
    assert isinstance(be.default, osb.HybridDefaultBackend)
    # 有 route → 权限路径校验（_all_paths_scoped_to_routes）才能通过
    assert sandbox.PERMISSION_ROUTE_PREFIX in be.routes
    assert be.routes[sandbox.PERMISSION_ROUTE_PREFIX] is be.default
    # 具备执行能力 → deepagents 会注册 execute 工具，并把 execute 路由到沙箱
    assert supports_execution(be) is True


def test_default_permissions_scoped_to_route(workspace, monkeypatch):
    """权限路径必须落在 route 前缀内，否则 FilesystemMiddleware 直接报错。

    审批范围收窄到"只碰本机才问"——

    - **没有本机挂载点**时默认权限是空列表（工作区 `/workspace/**` 已不需要审批）；
      空列表同样必须通过 route 校验（route 表本身非空即可）；
    - **有挂载点**时每条规则都是 interrupt，且路径都落在某个 route 前缀内。
    """
    from app.core.config import settings
    from deepagents.middleware.filesystem import _all_paths_scoped_to_routes

    # 环境 A：关掉本机访问（.env 默认是开的，必须显式关，否则会挂上真盘符）
    monkeypatch.setattr(settings, "harness_machine_access", False)
    monkeypatch.setattr(settings, "harness_machine_mounts", "")
    be = sandbox.build_main_backend(session_key="t", workspace=str(workspace))
    perms = sandbox.default_permissions()
    assert perms == [], "无本机挂载点 ⇒ 没有需要审批的路径"
    assert _all_paths_scoped_to_routes(perms, be) is True

    # 环境 B：挂一个本机目录
    monkeypatch.setattr(settings, "harness_machine_access", True)
    monkeypatch.setattr(settings, "harness_machine_mounts", f"probe={workspace.as_posix()}")
    be2 = sandbox.build_main_backend(session_key="t", workspace=str(workspace))
    perms2 = sandbox.default_permissions()
    assert perms2 and all(p.mode == "interrupt" for p in perms2)
    assert _all_paths_scoped_to_routes(perms2, be2) is True


def test_create_deep_agent_with_subagents_and_permissions(workspace, monkeypatch):
    """端到端回归：带子智能体时 create_deep_agent 不能再抛 NotImplementedError。

    这个组合曾经失败：主 Agent 权限 route 化之后，**子智能体**的权限路径（`/notes/`）
    仍在 route 外，deepagents 为 subagent 单独建 FilesystemMiddleware 时抛
    "does not yet support permissions with backends that provide command execution"。
    """
    monkeypatch.setenv("DEEPSEEK_API_KEY", "sk-test-placeholder")
    from deepagents import create_deep_agent

    from app.agent.model import build_llm
    from app.agent.subagents import load_all_subagents

    be = sandbox.build_main_backend(session_key="t", workspace=str(workspace))
    assert sandbox.validate_permissions(be) == []

    agent = create_deep_agent(
        model=build_llm(),
        name="t",
        backend=be,
        permissions=sandbox.default_permissions(),
        subagents=load_all_subagents(),
    )
    names = set(agent.nodes["tools"].bound._tools_by_name)
    assert {"execute", "write_file", "read_file", "task"} <= names


# --------------------------------------------------------------------------
# 2. 文件工具 → 本地磁盘
# --------------------------------------------------------------------------


def test_file_tools_hit_local_disk_without_sandbox(workspace, monkeypatch):
    """沙箱关闭时文件工具照常可用（本地磁盘），execute 返回可读错误。"""
    monkeypatch.setattr(settings, "sandbox_enabled", False)
    be = sandbox.build_main_backend(session_key="t", workspace=str(workspace))

    res = be.write("/workspace/notes.txt", "hello local disk")
    assert res.error is None
    assert (workspace / "notes.txt").read_text(encoding="utf-8") == "hello local disk"
    assert not (workspace / "workspace").exists(), "前缀没被折掉"

    assert be.read("/workspace/notes.txt").file_data["content"] == "hello local disk"
    assert [e["path"] for e in be.ls("/workspace").entries] == ["/workspace/notes.txt"]
    assert [m["path"] for m in be.glob("/workspace/*.txt", "/workspace").matches] == ["/workspace/notes.txt"]
    assert [m["path"] for m in (be.grep("hello", path="/workspace").matches or [])] == ["/workspace/notes.txt"]

    out = be.execute("echo hi")
    assert out.exit_code == 1
    assert "未启用" in out.output


def test_file_tools_do_not_touch_sandbox(workspace, fake_sandbox):
    """沙箱开启时，文件工具仍然只写本地磁盘（零远程往返）。"""
    be = sandbox.build_main_backend(session_key="t", workspace=str(workspace))
    be.write("/workspace/a.txt", "A")
    assert (workspace / "a.txt").read_text(encoding="utf-8") == "A"
    assert fake_sandbox.files.calls == [], "文件工具不该调用沙箱 files 服务"


# --------------------------------------------------------------------------
# 3. execute → 沙箱（含同步闭环）
# --------------------------------------------------------------------------


def test_execute_pushes_local_file_into_sandbox(workspace, fake_sandbox):
    """execute 前把本地文件推进容器（容器 cwd=/workspace，命令路径被翻译）。"""
    be = sandbox.build_main_backend(session_key="t", workspace=str(workspace))
    be.write("/workspace/data.txt", "42")

    out = be.execute("python -c \"print(open('/workspace/data.txt').read())\"")
    assert out.exit_code == 0
    assert "42" in out.output, "本地文件没被推进沙箱"

    exec_runs = [r for r in fake_sandbox.commands.runs if r[0].startswith("python -c")]
    assert exec_runs and exec_runs[-1][1] == CONTAINER_ROOT, "容器工作目录应为 /workspace"


def test_execute_pulls_sandbox_output_back_to_local_disk(workspace, fake_sandbox):
    """容器里产生的文件在执行后自动同步回本地磁盘（路径不带多余的 workspace 层）。"""
    be = sandbox.build_main_backend(session_key="t", workspace=str(workspace))

    out = be.execute(
        "python -c \"open('/workspace/answer.txt','w').write(str(42));print(open('/workspace/answer.txt').read())\""
    )
    assert out.exit_code == 0
    assert "42" in out.output

    local = workspace / "answer.txt"
    assert local.read_text(encoding="utf-8").strip() == "42", "沙箱产出没拉回本地"
    assert not (workspace / "workspace").exists(), "拉回路径多了一层 workspace"


def test_pulled_file_is_pushed_again_next_round(workspace, fake_sandbox):
    """同步闭环幂等：拉回的文件下一轮还能被读/推进沙箱，且不会重复拉。"""
    be = sandbox.build_main_backend(session_key="t", workspace=str(workspace))
    be.execute("python -c \"open('/workspace/x.txt','w').write(str(7));print(open('/workspace/x.txt').read())\"")
    assert (workspace / "x.txt").read_text(encoding="utf-8").strip() == "7"

    out = be.execute("python -c \"print(open('/workspace/x.txt').read())\"")
    assert "7" in out.output


def test_async_entrypoints(workspace, fake_sandbox):
    """异步入口（awrite/aread/aexecute）都要可用。"""

    async def _run():
        be = sandbox.build_main_backend(session_key="t", workspace=str(workspace))
        await be.awrite("/workspace/x.txt", "async")
        assert (await be.aread("/workspace/x.txt")).file_data["content"] == "async"
        out = await be.aexecute("python -c \"print(open('/workspace/x.txt').read())\"")
        assert "async" in out.output

    asyncio.run(_run())


# --------------------------------------------------------------------------
# 4. 路径映射与命令翻译
# --------------------------------------------------------------------------


def test_path_mapper_with_and_without_prefix():
    m_noprefix = osb.PathMapper(CONTAINER_ROOT)
    assert m_noprefix.to_sandbox("/a.txt") == "/workspace/a.txt"
    assert m_noprefix.to_sandbox("/") == "/workspace"
    assert m_noprefix.to_virtual("/workspace/a.txt") == "/a.txt"

    m_prefix = osb.PathMapper(CONTAINER_ROOT, prefix=CONTAINER_ROOT)
    assert m_prefix.to_sandbox("/workspace/a.txt") == "/workspace/a.txt"  # 不重复叠加
    assert m_prefix.to_sandbox("/a.txt") == "/workspace/a.txt"
    assert m_prefix.to_sandbox("/") == "/workspace"
    assert m_prefix.to_virtual("/workspace/a.txt") == "/workspace/a.txt"


def test_translate_command_rewrites_only_model_paths():
    assert osb.translate_command("cat /a.txt", CONTAINER_ROOT) == "cat /workspace/a.txt"
    # 已是容器路径 / 系统路径都不改
    assert osb.translate_command("cat /workspace/a.txt", CONTAINER_ROOT) == "cat /workspace/a.txt"
    assert osb.translate_command("ls /usr/bin", CONTAINER_ROOT) == "ls /usr/bin"
    assert osb.translate_command("echo / ", CONTAINER_ROOT) == "echo / "
