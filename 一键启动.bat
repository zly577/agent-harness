@echo off
title Agent Harness - One-Click Start
set "ROOT=D:\Deepseek_Harness\agent-harness"
set "PY=C:\Users\Administrator\anaconda3\envs\agent_env\python.exe"
set "BE_PORT=8000"

echo ================================================
echo    Agent Harness - One-Click Start
echo ================================================
echo.

rem ============================================================
rem  0. 先看后端端口是不是还被占着
rem ------------------------------------------------------------
rem  这是本脚本"看起来坏了"最常见的原因：上一次的后端窗口没关，
rem  或者 uvicorn --reload 的子进程成了孤儿还占着端口。
rem  这时新起的 uvicorn 会直接报 [Errno 10048] 绑定失败，
rem  窗口里一串英文报错 —— 而真正的原因跟代码一点关系都没有。
rem  与其让人猜，不如先查清楚 PID 再给个选择。
rem ============================================================
set "BE_PID="
for /f "tokens=5" %%p in ('netstat -ano ^| findstr ":%BE_PORT% " ^| findstr /i LISTENING') do (
    if not defined BE_PID set "BE_PID=%%p"
)
if not defined BE_PID goto :start_backend

echo [WARN] 端口 %BE_PORT% 已经被占用（PID=%BE_PID%），后端可能已经在运行。
echo        改完后端代码后，必须让那个进程重启才会生效。
echo.
set "ANSWER="
set /p "ANSWER=要结束它并重新启动后端吗？(Y/N) "
if /i "%ANSWER%"=="Y" (
    taskkill /F /T /PID %BE_PID% >nul 2>&1
    echo        已结束 PID %BE_PID%，等 2 秒让端口释放...
    rem [!] 这里必须写 timeout.exe 的**全路径**。
    rem 只写 `timeout /t 2` 的话，如果 PATH 里 Git Bash 的 /usr/bin 排在 System32
    rem 前面（装了 Git for Windows 的机器很常见），cmd 会找到 GNU coreutils 的
    rem timeout，报 `timeout: invalid time interval '/t'`，然后**根本不等待** ——
    rem 端口还没释放就起 uvicorn，又是 [Errno 10048]。脚本看起来"时好时坏"。
    "%SystemRoot%\System32\timeout.exe" /t 2 /nobreak >nul 2>&1
) else (
    echo        跳过后端启动，沿用已经在跑的那个实例。
    goto :frontend
)

:start_backend
if exist "%ROOT%\backend\app\main.py" (
    if exist "%PY%" (
        rem [!] 后端参数里**绝对不能出现通配符**（data/* 之类）。
        rem 原因（2026-09-18 实测踩坑）：uvicorn 的命令行是 click 解析的，而 click ≥ 8.0
        rem 在 Windows 上会"模拟 Unix shell"，对 sys.argv **每一项**做 glob 展开
        rem （click/core.py 的 windows_expand_args，utils._expand_args → glob(arg)）。
        rem 于是 --reload-exclude "data/*" 会被展开成 backend\data\ 下的真实文件：
        rem     Error: Got unexpected extra arguments (data\filesystem.sqlite data\harness.sqlite ...)
        rem 更阴的是它**时好时坏**：data/ 为空时 glob 返回空、click 回退成字面量，
        rem 一切正常；等 data/ 里攒了几个文件，脚本突然就起不来了 —— 正是"又坏了"。
        rem 加引号也没用：引号只管到 cmd 这一层，展开发生在 Python 进程内部。
        rem
        rem 所以改成**白名单**思路：用 --reload-dir 明确只监听 app/（不含通配符）。
        rem 不传 --reload-dir 时 uvicorn 监听整个 CWD（backend/），任何"运行时往
        rem backend/ 里写的文件"都会触发 reload，reload 又可能再写文件 —— 无限重启循环
        rem （P18 实测：4 分钟 41 份备份、后端一直重启、端口 LISTENING 但请求全 500）。
        rem
        rem [!] 后端日志靠 main.py 里的 FileHandler 落到仓库根 logs\backend.log，
        rem 不靠 shell 重定向（cmd /k 里嵌套 >> 的引号地狱太脆弱，且无法验证）。
        rem 这样不管怎么启动，日志都落得下盘，卡死/报错/StreamChunkTimeoutError 才看得到。
        start "AH-Backend" /D "%ROOT%\backend" cmd /k "%PY% -m uvicorn app.main:app --reload --reload-dir app"
        echo [OK] 后端窗口已启动（端口 %BE_PORT%，日志在 %ROOT%\logs\backend.log）
    ) else (
        echo [WARN] 找不到 conda 环境的 python: %PY%
    )
) else (
    echo [WARN] 找不到 backend\app\main.py，跳过后端
)

:frontend
if exist "%ROOT%\frontend\package.json" (
    start "AH-Frontend" /D "%ROOT%\frontend" cmd /k "npm run dev"
    echo [OK] 前端窗口已启动（Vite）
) else (
    echo [WARN] 找不到 frontend\package.json，跳过前端
)

if exist "%ROOT%\_probe\sandbox_tunnel.ps1" (
    start "AH-Tunnel" /D "%ROOT%\_probe" powershell -NoExit -ExecutionPolicy Bypass -File "%ROOT%\_probe\sandbox_tunnel.ps1"
    echo [OK] 隧道窗口已启动
) else (
    echo [WARN] 找不到 _probe\sandbox_tunnel.ps1，跳过隧道
)

echo.
echo 三个服务分别在独立窗口里运行，关掉对应窗口即可停止：
echo   1. Backend  : uvicorn app.main:app --reload --reload-dir app  （日志 → logs\backend.log）
echo   2. Frontend : npm run dev  (Vite)
echo   3. Tunnel   : 127.0.0.1:18080 -^> 39.105.158.228:8080
echo.
echo 提示：改完后端代码后，如果只改了 .py，--reload 会自己重启；
echo       但如果改了 .env（比如 MCP_ENABLED），--reload **不会**触发，要手动重启。
echo.
pause
