"""Native Windows shells are exercised in temporary workspaces, without models."""
import asyncio
import os
import sys
from pathlib import Path

import pytest

from app.agent import host_shell
from app.agent.host_shell import HostShellBackend, decode_shell_output, resolve_windows_shell


@pytest.fixture(params=["powershell", "pwsh"])
def backend(request, tmp_path):
    if os.name != "nt":
        pytest.skip("Native Windows PowerShell test")
    try:
        resolve_windows_shell(request.param)
    except FileNotFoundError:
        pytest.skip(f"{request.param} is not installed")
    root = tmp_path / "中文 空格项目"
    root.mkdir()
    return HostShellBackend(root_dir=root, windows_shell=request.param)


def psquote(value):
    return "'" + str(value).replace("'", "''") + "'"


def test_powershell_objects_unicode_paths_and_pipeline(backend):
    result = backend.execute("Set-Content -LiteralPath './中文 文件.txt' -Value '你好 Windows' -Encoding UTF8\n"
                             "Get-ChildItem -LiteralPath . | Where-Object { $_.Name -like '*.txt' } | Select-Object Name\n"
                             "Write-Output (Get-Location).Path\nGet-Content -LiteralPath './中文 文件.txt' -Encoding UTF8")
    assert result.exit_code == 0, result.output
    assert "中文 文件.txt" in result.output and "你好 Windows" in result.output
    assert str(backend.cwd) in result.output


def test_native_cli_env_stderr_and_exit_code(backend):
    script = Path(backend.cwd) / "原生 命令.py"
    script.write_text("import os,sys\nprint('中文 CLI ' + os.environ['HARNESS_TEST_VALUE'])\n"
                      "print('stderr 中文',file=sys.stderr)\nsys.exit(7)\n", encoding="utf-8")
    result = backend.execute("$env:HARNESS_TEST_VALUE='环境'\n& " + psquote(sys.executable) + " " + psquote(script))
    assert result.exit_code == 7, result.output
    assert "中文 CLI 环境" in result.output
    assert "[stderr]" in result.output and "stderr 中文" in result.output


@pytest.mark.parametrize("command", ["Get-Item -LiteralPath './missing'", "throw 'failed'", "This_Command_Does_Not_Exist_123"])
def test_powershell_failures_are_nonzero(backend, command):
    result = backend.execute(command)
    assert result.exit_code != 0 and "[stderr]" in result.output


def test_native_nonzero_and_explicit_exit_are_preserved(backend):
    assert backend.execute('cmd.exe /d /c "exit 13"').exit_code == 13
    result = backend.execute("Write-Output 'before-exit'; exit 9")
    assert result.exit_code == 9 and "before-exit" in result.output


def test_cmd_builtins_and_batch_can_be_called_from_powershell(backend):
    batch = Path(backend.cwd) / "本机 脚本.cmd"
    batch.write_text("@echo off\necho batch-cli-ok\nexit /b 4\n", encoding="ascii")
    result = backend.execute("cmd.exe /d /c " + psquote('"' + str(batch) + '"'))
    assert result.exit_code == 4 and "batch-cli-ok" in result.output
    assert backend.execute("cmd.exe /d /c 'echo cmd-cli-ok'").exit_code == 0


def test_large_multiline_script_avoids_windows_commandline_limit(backend):
    command = ("# 多行脚本与引号 ' \" 不经过 cmd 解析\n" * 2000) + "Write-Output 'long-script-ok'"
    result = backend.execute(command)
    assert result.exit_code == 0 and "long-script-ok" in result.output


def test_shell_state_is_not_shared_between_calls(backend):
    assert backend.execute("$env:HARNESS_ONLY_THIS_CALL='one'").exit_code == 0
    result = backend.execute("if (Test-Path env:HARNESS_ONLY_THIS_CALL) { exit 3 }; Write-Output 'clean' ")
    assert result.exit_code == 0 and "clean" in result.output


def test_timeout_preserves_partial_output(backend):
    result = backend.execute("Write-Output 'before-timeout'; Start-Sleep -Seconds 30", timeout=2)
    assert result.exit_code == 124, result.output
    assert "before-timeout" in result.output and "timed out" in result.output


def test_async_cancel_stops_its_native_child(backend):
    script = Path(backend.cwd) / "child.py"
    script.write_text("import pathlib,time,os\npathlib.Path('started').write_text(str(os.getpid()))\n"
                      "time.sleep(4)\npathlib.Path('finished').write_text('unexpected')\n", encoding="utf-8")
    async def scenario():
        task = asyncio.create_task(backend.aexecute("& " + psquote(sys.executable) + " " + psquote(script)))
        try:
            for _ in range(100):
                if (Path(backend.cwd) / "started").exists():
                    break
                await asyncio.sleep(.05)
            assert (Path(backend.cwd) / "started").exists()
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
            # This process belongs to this test, not a process name shared by users.
            pid = int((Path(backend.cwd) / "started").read_text())
            import ctypes
            from ctypes import wintypes
            ctypes.windll.kernel32.OpenProcess.restype = wintypes.HANDLE
            ctypes.windll.kernel32.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
            ctypes.windll.kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
            process = ctypes.windll.kernel32.OpenProcess(0x00100000, False, pid)
            if process:
                try:
                    assert ctypes.windll.kernel32.WaitForSingleObject(process, 0) == 0
                finally:
                    ctypes.windll.kernel32.CloseHandle(process)
            assert not (Path(backend.cwd) / "finished").exists()
        finally:
            if not task.done():
                task.cancel()
                try:
                    await task
                except asyncio.CancelledError:
                    pass
    asyncio.run(scenario())


@pytest.mark.skipif(os.name != "nt", reason="Native cmd test")
def test_cmd_mode_supports_unicode_and_errorlevel(tmp_path):
    backend = HostShellBackend(root_dir=tmp_path, windows_shell="cmd")
    result = backend.execute("echo 中文 cmd\nexit /b 6")
    assert result.exit_code == 6 and "中文 cmd" in result.output


def test_resolver_prefers_pwsh_and_respects_explicit_choice(monkeypatch):
    monkeypatch.setattr(host_shell.shutil, "which", lambda name, **kwargs: "/tools/" + name)
    assert resolve_windows_shell("auto").kind == "pwsh"
    assert resolve_windows_shell("powershell").kind == "powershell"
    assert resolve_windows_shell("cmd").kind == "cmd"
    with pytest.raises(ValueError):
        resolve_windows_shell("bash")


def test_resolver_falls_back_to_windows_powershell(monkeypatch):
    monkeypatch.setattr(host_shell.shutil, "which", lambda name, **kwargs: "/tools/powershell.exe" if name == "powershell.exe" else None)
    assert resolve_windows_shell("auto").kind == "powershell"


def test_utf8_and_legacy_encoding_are_decoded(monkeypatch):
    monkeypatch.setattr(host_shell.locale, "getencoding", lambda: "cp936")
    assert decode_shell_output("中文".encode("utf-8")) == "中文"
    assert decode_shell_output("中文".encode("gbk")) == "中文"
    assert decode_shell_output("中文".encode("utf-16")) == "中文"


def test_output_limit_counts_utf8_bytes(tmp_path):
    backend = HostShellBackend(root_dir=tmp_path, max_output_bytes=7)
    result = backend._response("中文中文".encode("utf-8"), b"", 0)
    assert result.truncated and result.output.startswith("中文\n")


def test_native_large_stdout_and_stderr_are_drained_with_a_limit(backend):
    script = Path(backend.cwd) / "large-output.py"
    script.write_text("import sys\nsys.stdout.buffer.write(b'x' * 2_000_000)\n"
                      "sys.stdout.flush()\nsys.stderr.buffer.write(b'y' * 2_000_000)\n"
                      "sys.stderr.flush()\nsys.exit(7)\n", encoding="utf-8")
    backend._max_output_bytes = 1000
    result = backend.execute("& " + psquote(sys.executable) + " " + psquote(script), timeout=20)
    assert result.exit_code == 7, result.output
    assert result.truncated and result.output.startswith("x" * 1000)
    assert "Output truncated at 1000 bytes" in result.output
    assert len(result.output.encode("utf-8")) < 1200


def test_positive_timeout_and_nonempty_command(tmp_path):
    backend = HostShellBackend(root_dir=tmp_path)
    assert backend.execute("  ").exit_code == 1
    with pytest.raises(ValueError):
        backend.execute("echo test", timeout=0)
    with pytest.raises(ValueError):
        HostShellBackend(root_dir=tmp_path, timeout=0)


def test_posix_arguments_stay_on_posix_shell(tmp_path, monkeypatch):
    monkeypatch.setattr(host_shell, "is_windows", lambda: False)
    backend = HostShellBackend(root_dir=tmp_path)
    args, use_shell = backend._command_args("printf '%s' linux", tmp_path)
    assert args == "printf '%s' linux" and use_shell
    assert "POSIX" in backend.execute_description


def test_model_tool_description_and_schema_match_native_shell(tmp_path):
    backend = HostShellBackend(root_dir=tmp_path)
    middleware = host_shell.host_filesystem_middleware(backend)[0]
    tool = next(tool for tool in middleware.tools if tool.name == "execute")
    assert backend.shell_description in tool.description
    assert "isolated sandbox" not in tool.description
    assert backend.shell_description in tool.args_schema.model_fields["command"].description


def test_configured_subagent_gets_native_shell_guidance(tmp_path):
    from app.agent.subagents import SubAgentConfig, build_subagent
    backend = HostShellBackend(root_dir=tmp_path)
    spec = build_subagent(SubAgentConfig(name="worker", description="test", system_prompt="test"),
                          mode="full", workspace=tmp_path, backend=backend)
    assert backend.shell_description in spec["system_prompt"]
    fs = next(m for m in spec["middleware"] if m.name == "FilesystemMiddleware")
    assert "execute" in {tool.name for tool in fs.tools}


@pytest.mark.parametrize("worker", ["general-purpose", "worker"])
def test_deepagent_and_subagent_use_the_native_execute_tool(tmp_path, worker):
    from deepagents import create_deep_agent
    from langchain_core.language_models.fake_chat_models import FakeMessagesListChatModel
    from langchain_core.messages import AIMessage
    from app.agent.sandbox import build_main_backend
    from app.agent.subagents import SubAgentConfig, build_subagent

    bound = []
    class Model(FakeMessagesListChatModel):
        def bind_tools(self, tools, **kwargs):
            bound.extend(tool for tool in tools if getattr(tool, "name", "") == "execute")
            return self

    backend = build_main_backend(workspace=str(tmp_path), mode="full")
    command = "Set-Content -LiteralPath './child-result.txt' -Value 'native-child-ok'" if os.name == "nt" else "printf native-child-ok > child-result.txt"
    model = Model(responses=[
        AIMessage(content="", tool_calls=[{"name": "task", "args": {"subagent_type": worker, "description": "run native command"}, "id": "delegate"}]),
        AIMessage(content="", tool_calls=[{"name": "execute", "args": {"command": command}, "id": "execute"}]),
        AIMessage(content="child done"), AIMessage(content="all done"),
    ])
    subagent = build_subagent(SubAgentConfig(name="worker", description="worker", system_prompt="worker"),
                             mode="full", backend=backend, workspace=tmp_path)
    graph = create_deep_agent(model=model, backend=backend, subagents=[subagent],
                              middleware=host_shell.host_filesystem_middleware(backend))
    asyncio.run(graph.ainvoke({"messages": [{"role": "user", "content": "run"}]}))
    assert (tmp_path / "child-result.txt").read_text(encoding="utf-8-sig").strip() == "native-child-ok"
    assert len(bound) >= 2
    assert all(backend.default.shell_description in tool.description for tool in bound)
