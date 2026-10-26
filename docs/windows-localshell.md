# Windows LocalShell 适配说明

## 执行方式

完全访问模式的本机执行器现在位于 `backend/app/agent/host_shell.py`。原来的 `opensandbox_backend.HostShellBackend`、`decode_shell_output` 导入入口继续可用。

Windows 下默认优先使用 PowerShell 7，找不到时使用 Windows PowerShell；也可以固定使用 cmd。Linux 后端继续使用 POSIX shell。

这里判断的是**后端进程所在的操作系统**。直接在 Windows 运行后端时使用 Windows shell；Windows 上的 Linux Docker 容器仍执行 Linux 命令，不能直接调用宿主机 PowerShell。

## 配置

在本机运行使用的 `backend/.env` 中，可设置：

```dotenv
HARNESS_WINDOWS_SHELL=auto
```

| 值 | Windows 下的行为 |
|---|---|
| `auto` | 优先查找 `pwsh.exe`，其次 `powershell.exe` |
| `pwsh` | 固定使用 PowerShell 7，找不到时明确报错 |
| `powershell` | 固定使用 Windows PowerShell，兼容系统自带的 5.1 |
| `cmd` | 使用 `cmd.exe` 解释命令，支持传统 cmd 和批处理语法 |

修改后重启后端，并在界面切换到「完全访问」。这个配置只选择 Windows 的解释程序，不改变权限模式和 Linux 部署行为。

## 方法的具体调整

| 部分 | 实现 |
|---|---|
| shell 选择 | `resolve_windows_shell()` 按配置查找程序；auto 不会静默退回 cmd |
| Windows 进程启动 | 使用可执行文件与参数列表，不再用 `shell=True` 将所有命令交给 cmd 解析 |
| PowerShell 参数 | `-NoLogo -NoProfile -NonInteractive -ExecutionPolicy Bypass -File` |
| 中文与长命令 | 命令写入 UTF-8 BOM 临时脚本，避免多层引号转义与 Windows 命令行长度限制 |
| 输出编码 | 设置 PowerShell 控制台和 Python 的 UTF-8 输出；字节解码保留系统代码页回退，并支持 UTF-16 BOM |
| 对象格式化 | 先通过 `Out-Default` 完成对象输出，再处理退出码，避免 `exit` 吞掉表格输出 |
| 错误与退出码 | 保留原生 CLI 的 `$LASTEXITCODE` 和显式 `exit N`；普通 PowerShell cmdlet 错误返回非零退出码 |
| 工作目录 | 每次从当前会话的真实工作区开始，支持中文与空格目录 |
| 标准输入与窗口 | 关闭标准输入，后台启动，不弹出命令窗口 |
| 超时 | 返回退出码 124，保留已收到的输出，并尝试终止本次 PID 对应的进程树 |
| 异步取消 | `aexecute()` 通知执行线程终止本次命令及正常从属进程，再完成资源收尾 |
| 输出截断 | 按 UTF-8 字节数限制输出，默认 100,000 字节 |

`ExecutionPolicy Bypass` 仅作为本次进程的启动参数，没有执行修改系统持久执行策略的命令。

临时 PowerShell 脚本显式使用 BOM，是为了兼容 Windows PowerShell 5.1 的编码处理；原生命令退出码也显式传回进程退出码。参考微软的[字符编码说明](https://learn.microsoft.com/en-us/powershell/module/microsoft.powershell.core/about/about_character_encoding?view=powershell-7)和 [PowerShell 启动参数说明](https://learn.microsoft.com/en-us/powershell/module/microsoft.powershell.core/about/about_powershell_exe?view=powershell-5.1)。

## 命令示例

默认 PowerShell 模式：

```powershell
Get-ChildItem -LiteralPath .
Get-Content -LiteralPath './中文 文件.txt' -Encoding UTF8
$env:APP_MODE = 'development'
python ./script.py
git status --short
& 'C:/Program Files/SomeTool/tool.exe' --version
```

需要 cmd 内置命令或旧批处理语法时，可以显式调用：

```powershell
cmd.exe /d /c 'dir /b'
cmd.exe /d /c 'echo cmd-ok'
```

也可以配置 `HARNESS_WINDOWS_SHELL=cmd`，此时 `command` 整体按 cmd 语法解释。两种语法不做自动转换，也不会把 Linux 命令机械替换成 Windows 命令。

兼容 Windows PowerShell 5.1 时，多条语句使用换行或分号，不依赖 `&&`、`||`。需要让下一步依赖原生程序成功时，显式判断 `$LASTEXITCODE`。复杂的 Python/Node 代码优先写入工作区脚本后执行，避免多层命令行引号。

文件工具继续使用 `/workspace/...`、`/mnt/...` 虚拟路径；命令使用工作区相对路径或 Windows 真实路径，例如 `D:/项目/script.py`。执行器不会自动改写命令里的虚拟路径。

每次调用启动新 shell，变量、函数和切换目录不会跨调用保留，也不会加载 PowerShell profile。

## 主智能体与子智能体

框架原有的 execute 工具说明描述的是隔离沙箱，包含类 Unix 命令示例。仅更换底层进程，仍可能让模型选错语法。

本轮在完全访问模式替换了对应文件工具中间件的 execute 描述和 command 参数说明，使模型看到真实 shell、工作目录与命令语法。主智能体、配置的子智能体以及框架默认的 general-purpose 子智能体都通过测试验证了原生命令执行链路。沙箱模式保留原来的工具说明和 OpenSandbox 流程。

## 验证与边界

`backend/tests/test_host_shell.py` 覆盖 PowerShell 5.1、PowerShell 7、cmd、中文路径、对象输出、管道、长脚本、环境变量、本机 CLI、stderr、退出码、批处理、超时、取消和子智能体调用。

在 `backend` 目录执行专项测试：

```powershell
python -m pytest tests/test_host_shell.py tests/test_p21_access_mode.py tests/test_desktop_modes.py -o addopts='' -q
```

当前 Windows 机器上，这组专项测试 **56 项通过**，实际执行了已安装的 Windows PowerShell、PowerShell 7 和 cmd。未安装某个 PowerShell 版本的环境会跳过该版本的原生测试。

本实现用于无交互命令与脚本，不包含交互式终端会话或 GUI 自动化。超时和取消针对本次命令的正常进程树；自行脱离进程树或交给外部服务管理的进程，不能据此保证终止。部分旧 CLI 自行输出固定代码页，仍可能需要在命令中指定编码，或使用 cmd 模式处理原始输出。
