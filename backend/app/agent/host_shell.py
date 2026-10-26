"""Native host execution: PowerShell/cmd on Windows, POSIX shell elsewhere."""
from __future__ import annotations

import asyncio
import locale
import os
import shutil
import signal
import subprocess
import tempfile
import threading
import time
from dataclasses import dataclass
from pathlib import Path

from deepagents.backends import DEFAULT_EXECUTE_TIMEOUT, LocalShellBackend
from deepagents.backends.protocol import ExecuteResponse

from app.agent.path_mapping import PathMapper
from app.agent.workspace_files import WorkspacePrefixMixin
from app.core.config import settings


def is_windows() -> bool:
    return os.name == "nt"


@dataclass(frozen=True)
class WindowsShell:
    kind: str
    executable: str

    @property
    def label(self) -> str:
        return {"pwsh": "PowerShell 7（pwsh.exe）", "powershell": "Windows PowerShell（powershell.exe，兼容 5.1）", "cmd": "Windows 命令提示符（cmd.exe）"}[self.kind]


def resolve_windows_shell(choice: str = "auto", env: dict[str, str] | None = None) -> WindowsShell:
    """Resolve an executable, preferring pwsh in auto mode, then Windows PowerShell."""
    environment = os.environ if env is None else env
    if choice not in {"auto", "pwsh", "powershell", "cmd"}:
        raise ValueError(f"不支持的 Windows shell：{choice}")
    system_root = Path(environment.get("SystemRoot") or os.environ.get("SystemRoot") or r"C:\Windows")
    candidates = ("pwsh", "powershell") if choice == "auto" else (choice,)
    for kind in candidates:
        executable = shutil.which(f"{kind}.exe", path=environment.get("PATH", ""))
        if executable:
            return WindowsShell(kind, executable)
        fallback = system_root / "System32" / ("WindowsPowerShell/v1.0/powershell.exe" if kind == "powershell" else "cmd.exe")
        if kind != "pwsh" and fallback.is_file():
            return WindowsShell(kind, str(fallback))
    raise FileNotFoundError(f"找不到 Windows shell（{choice}）；请安装 PowerShell 或配置 HARNESS_WINDOWS_SHELL=cmd")


def decode_shell_output(raw: bytes | None) -> str:
    """Prefer UTF-8, then the native Windows ANSI code page; never drop output."""
    if not raw:
        return ""
    if raw.startswith((b"\xff\xfe", b"\xfe\xff")):
        return raw.decode("utf-16", errors="replace")
    try:
        return raw.decode("utf-8-sig")
    except UnicodeDecodeError:
        try:
            fallback = locale.getencoding()
        except Exception:
            fallback = "utf-8"
        return raw.decode(fallback, errors="replace")


_POWERSHELL_PREFIX = r"""$ErrorActionPreference = 'Stop'
$ProgressPreference = 'SilentlyContinue'
$PSNativeCommandUseErrorActionPreference = $false
$OutputEncoding = [Console]::InputEncoding = [Console]::OutputEncoding = New-Object System.Text.UTF8Encoding($false)
if (Test-Path variable:PSStyle) { $PSStyle.OutputRendering = 'PlainText' }
$global:LASTEXITCODE = 0
try {
"""
_POWERSHELL_SUFFIX = r"""
$__harnessCommandSucceeded = $?
if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }
if (-not $__harnessCommandSucceeded) { exit 1 }
exit 0
} catch {
    [Console]::Error.WriteLine(($_ | Out-String))
    exit 1
}
"""


class _BoundedCapture:
    """Drain a pipe without retaining unbounded command output in memory."""

    def __init__(self, stream, limit: int):
        self.stream, self.limit = stream, limit
        self.data = bytearray()
        self.total = 0
        self.error = None
        self.done = threading.Event()
        self.lock = threading.Lock()
        self.thread = threading.Thread(target=self._drain, name="harness-shell-output", daemon=True)
        self.thread.start()

    def _drain(self):
        try:
            while chunk := self.stream.read1(8192):
                with self.lock:
                    self.total += len(chunk)
                    remaining = self.limit - len(self.data)
                    if remaining > 0:
                        self.data.extend(chunk[:remaining])
        except (OSError, ValueError) as exc:
            self.error = exc
        finally:
            self.stream.close()
            self.done.set()

    def snapshot(self):
        with self.lock:
            return bytes(self.data), self.total > len(self.data)


class HostShellBackend(WorkspacePrefixMixin, LocalShellBackend):
    """Full-access file backend with native, noninteractive host command execution.

    Windows commands are scripts, not command text sent through cmd.exe's parser.
    PowerShell scripts use UTF-8 BOM for 5.1 and 7; cmd scripts set code page 65001.
    Every invocation starts in cwd, with a fresh shell and no persistent session.
    """

    def __init__(self, *, root_dir: str | Path, virtual_mode: bool = True,
                 timeout: int | None = None, inherit_env: bool = True,
                 env: dict[str, str] | None = None, prefix: str | None = None,
                 windows_shell: str | None = None, max_output_bytes: int = 100_000) -> None:
        default_timeout = timeout if timeout is not None else (settings.sandbox_default_timeout_seconds or DEFAULT_EXECUTE_TIMEOUT)
        if max_output_bytes <= 0:
            raise ValueError("max_output_bytes must be positive")
        super().__init__(root_dir=root_dir, virtual_mode=virtual_mode,
                         timeout=default_timeout, inherit_env=inherit_env, env=env,
                         max_output_bytes=max_output_bytes)
        self._prefix = PathMapper(prefix) if prefix else None
        self._windows = is_windows()
        self._windows_shell = windows_shell or settings.harness_windows_shell
        self._env.setdefault("PYTHONIOENCODING", "utf-8")
        self._env.setdefault("PYTHONUTF8", "1")
        if self._windows:
            self._env.setdefault("SystemRoot", os.environ.get("SystemRoot", r"C:\Windows"))

    @property
    def shell_description(self) -> str:
        return resolve_windows_shell(self._windows_shell, self._env).label if self._windows else "POSIX shell（/bin/sh）"

    @property
    def execute_description(self) -> str:
        details = windows_command_guidance(self._windows_shell) if self._windows else "使用 POSIX shell 语法。"
        return (
            f"在后端本机通过 {self.shell_description} 执行命令，不是隔离沙箱。\n"
            f"{details}\n工作目录为 {self.cwd}；使用工作区相对路径或本机真实路径，不使用 /workspace、/mnt 虚拟路径。"
            "多行脚本可直接传入 command。每次调用使用新进程，变量和 cd 不跨调用保留。"
            "返回标准输出、带 [stderr] 标记的错误输出和退出码；timeout 必须为正秒数。"
            "查找、搜索和读取文件优先用 ls/glob/grep/read_file 工具。"
        )

    def execute(self, command: str, *, timeout: int | None = None) -> ExecuteResponse:
        return self._execute(command, timeout=timeout)

    async def aexecute(self, command: str, *, timeout: int | None = None) -> ExecuteResponse:
        cancelled = threading.Event()
        task = asyncio.create_task(asyncio.to_thread(self._execute, command, timeout=timeout, cancelled=cancelled))
        try:
            return await asyncio.shield(task)
        except asyncio.CancelledError:
            cancelled.set()
            # Wait for the worker to terminate its own process tree and close pipes.
            await asyncio.shield(task)
            raise

    def _command_args(self, command: str, directory: Path):
        if not self._windows:
            return command, True
        shell = resolve_windows_shell(self._windows_shell, self._env)
        if shell.kind == "cmd":
            script = directory / "command.cmd"
            script.write_bytes(("@echo off\r\nchcp 65001 >nul\r\n" + command + "\r\n").encode("utf-8"))
            return [shell.executable, "/d", "/s", "/c", str(script)], False
        script = directory / "command.ps1"
        user_script = directory / "user.ps1"
        user_script.write_text(command, encoding="utf-8-sig")
        # Flush PowerShell's object formatter before exiting. Appending exit to
        # the user's code directly can swallow Get-Location/Get-ChildItem output.
        invocation = "& '" + str(user_script).replace("'", "''") + "' | Out-Default"
        script.write_text(_POWERSHELL_PREFIX + invocation + _POWERSHELL_SUFFIX, encoding="utf-8-sig")
        return [shell.executable, "-NoLogo", "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass", "-File", str(script)], False

    def _terminate(self, process: subprocess.Popen) -> None:
        if self._windows:
            taskkill = Path(self._env["SystemRoot"]) / "System32" / "taskkill.exe"
            try:
                subprocess.run([str(taskkill), "/PID", str(process.pid), "/T", "/F"],
                               stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                               creationflags=subprocess.CREATE_NO_WINDOW, timeout=5, check=False)
            except (OSError, subprocess.TimeoutExpired):
                pass  # Fall back to killing the process we own below.
        else:
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
        if process.poll() is None:
            process.kill()

    def _execute(self, command: str, *, timeout=None, cancelled: threading.Event | None = None) -> ExecuteResponse:
        if not isinstance(command, str) or not command.strip():
            return ExecuteResponse(output="Error: Command must be a non-empty string.", exit_code=1, truncated=False)
        effective_timeout = self._default_timeout if timeout is None else timeout
        if effective_timeout <= 0:
            raise ValueError(f"timeout must be positive, got {effective_timeout}")
        process = None
        captures = []
        try:
            with tempfile.TemporaryDirectory(prefix="harness-shell-") as temporary:
                args, shell = self._command_args(command, Path(temporary))
                options = {"creationflags": subprocess.CREATE_NO_WINDOW | subprocess.CREATE_NEW_PROCESS_GROUP} if self._windows else {"start_new_session": True}
                process = subprocess.Popen(args, shell=shell, cwd=str(self.cwd), env=self._env,
                                           stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                           **options)
                captures = [_BoundedCapture(process.stdout, self._max_output_bytes),
                            _BoundedCapture(process.stderr, self._max_output_bytes)]
                deadline = time.monotonic() + effective_timeout
                reason = None
                while True:
                    if cancelled and cancelled.is_set():
                        reason = "Error: Command cancelled."
                        break
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        reason = f"Error: Command timed out after {effective_timeout} seconds."
                        break
                    if process.poll() is not None and all(capture.done.is_set() for capture in captures):
                        break
                    if process.poll() is None:
                        try:
                            process.wait(timeout=min(remaining, 0.1))
                        except subprocess.TimeoutExpired:
                            pass
                    else:
                        captures[0].done.wait(min(remaining, 0.05))
                        captures[1].done.wait(min(remaining, 0.05))
                if reason:
                    self._terminate(process)
                for capture in captures:
                    capture.thread.join(timeout=1)
                stdout, out_truncated = captures[0].snapshot()
                stderr, err_truncated = captures[1].snapshot()
                code = (130 if cancelled and cancelled.is_set() else 124) if reason else process.returncode
                if any(capture.error for capture in captures):
                    code, reason = 1, "Error: Could not read command output."
                return self._response(stdout, stderr, code, reason, truncated=out_truncated or err_truncated)
        except Exception as exc:
            return ExecuteResponse(output=f"Error: executing command ({type(exc).__name__}): {exc}", exit_code=1, truncated=False)
        finally:
            if process is not None:
                if process.poll() is None:
                    process.kill()
                    process.wait(timeout=5)
                if not captures:
                    for stream in (process.stdout, process.stderr):
                        if stream:
                            stream.close()

    def _response(self, stdout: bytes, stderr: bytes, code: int, reason: str | None = None, *, truncated: bool = False) -> ExecuteResponse:
        parts = [reason] if reason else []
        if stdout:
            parts.append(decode_shell_output(stdout))
        if stderr:
            parts.extend(f"[stderr] {line}" for line in decode_shell_output(stderr).strip().splitlines())
        output = "\n".join(parts) or "<no output>"
        encoded = output.encode("utf-8")
        truncated = truncated or len(encoded) > self._max_output_bytes
        if truncated:
            output = encoded[:self._max_output_bytes].decode("utf-8", errors="ignore")
            output += f"\n\n... Output truncated at {self._max_output_bytes} bytes."
        if code:
            output = f"{output.rstrip()}\n\nExit code: {code}"
        return ExecuteResponse(output=output, exit_code=code, truncated=truncated)


def windows_command_guidance(choice: str = "auto") -> str:
    if choice == "cmd":
        return "使用 cmd.exe 语法：dir /b、type、%NAME%、where.exe；依赖成功的命令可用 && 串联。PowerShell cmdlet 必须显式通过 powershell.exe -NoProfile -NonInteractive -Command 调用。"
    return (
        "使用 PowerShell 语法：Get-ChildItem、Get-Content -Encoding UTF8、Select-String、$env:NAME、Where-Object。"
        "支持直接运行 python、git、node 等已安装且位于 PATH 的 CLI；带空格的可执行文件路径使用 & 'C:/Program Files/.../tool.exe'。"
        "cmd 内置命令或 .bat/.cmd 脚本可显式使用 cmd.exe /d /c 'dir /b'；where 请写 where.exe。"
        "兼容 Windows PowerShell 5.1 时不要使用 &&、||、Bash heredoc、export、source、/bin/bash 或 Linux 参数；"
        "PowerShell 多条语句用换行或分号，原生程序需要依赖成功时检查 $LASTEXITCODE。"
        "写 UTF-8 文件请显式指定 -Encoding UTF8。"
    )


def host_filesystem_middleware(backend, permissions=None):
    """Replace the built-in sandbox execute description using the same FS slot."""
    from deepagents.middleware.filesystem import FilesystemMiddleware
    from pydantic import Field, create_model
    default = getattr(backend, "default", backend)
    if not isinstance(default, HostShellBackend):
        return []
    middleware = FilesystemMiddleware(backend=backend, _permissions=permissions or [],
                                     custom_tool_descriptions={"execute": default.execute_description})
    for tool in middleware.tools:
        if tool.name == "execute":
            tool.args_schema = create_model(
                "HostExecuteInput", __base__=tool.args_schema,
                command=(str, Field(description=f"在本机 {default.shell_description} 中执行的命令或多行脚本。")),
            )
    return [middleware]
