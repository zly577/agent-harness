# P22 运行可见性（Run Visibility）设计

> 来源：2026-10-09 用户截图 —— 工具卡「用 9000 端口重启 auth_app 服务」+ 命令
> `Stop-Process -Name python -Force -ErrorAction Silen…` + 右列「进行中」，
> **一等就是很久，没有任何反馈**。

## 一、现象与现场（先把它变成可复现的事实）

| 时间 | 事实 |
|---|---|
| 17:21:23 | 会话 `d917…` 里 `PowerShell` 工具执行 `Stop-Process -Name python -Force …`（`tool_call_logs#1775`） |
| 17:21 之后 | `logs/backend.log` **完全静默** —— 后端进程已经没了 |
| 17:30:24 | 重新拉起后端（上一轮的排查动作） |
| 17:31:45 | 同一条命令又跑了一次，这次换 9000 端口（`tool_call_logs#1797`） |
| 17:31:42 之后 | 日志再次停住；`netstat :8000` 无 LISTENING、`tasklist` 无 python |
| 现在 | `#1797` 仍停在 `status='running'`（截图里那张"进行中"卡片就是它） |

**根因不是"在慢慢跑"，而是这条命令把跑它的后端一起杀了。**

`Stop-Process -Name python -Force` 会结束**所有**名为 python 的进程，而这个 harness
的后端自己就是 `python.exe`（uvicorn）。后端一死：

1. 那一轮 `run` 不可能再产出 `tool_result` ⇒ `tool_call_logs` 永远停在 `running`；
2. SSE 只是"安静地断掉"，前端不会收到任何错误；
3. 前端收尾逻辑**只收尾了 delegations，漏了 toolCalls** ⇒ 卡片一直"进行中"。

## 二、三层成因，逐层堵

| 层 | 成因 | 处置 |
|---|---|---|
| 模型 | 用"按进程名批量杀"来重启服务 | `SelfKillGuardMiddleware` 硬拦截（提示词也写了红线，但提示词只是建议） |
| 前端 | 流结束时只收尾委派、不收尾工具调用；也没有"静默多久"的判据 | `finishTurn()` + `runLiveness()` |
| 后端 | `tool_result` 不带成败；回放一律按 `done` 渲染；只有"发新消息"才收尾遗留 running | 事件加 `ok/exit_code/reason/duration_ms`；`_merge_tool_log` 带状态；`load_history` 也收尾 |

## 三、做了什么

### 1. 硬护栏（后端）

`app/agent/middleware.py::SelfKillGuardMiddleware`
- 命中"按进程名杀 python"这一族（`Stop-Process -Name python` / `taskkill /IM python.exe` /
  `pkill python` / `killall python` / `wmic … delete` / `Get-Process -Name python | Stop-Process`）；
- 参数**递归**扫描（命令可能藏在 MCP 工具任意嵌套结构里）；
- 命中即短路，`handler` 一次都不调用，回一条以 `Error:` 开头的 `ToolMessage`
  （⇒ 判定为失败、卡片显示"失败"），`status="success"` 是为了**不**触发熔断；
- 主 Agent 与**每个子智能体**都单独挂（子智能体不继承主 Agent 中间件栈）。
- **不误伤**：`Stop-Process -Id <PID>`（按端口定位后精确结束）是我们推荐的正确做法，
  绝不能拦。

### 2. 成败判定（后端，纯函数）

`app/agent/tool_outcome.py::classify_tool_result(name, content) -> {ok, exit_code, reason}`
- 顺序：**先认退出码尾巴** `[Command failed/succeeded with exit code N]` → 再认 `Error:` 前缀
  → 都不命中才落成功；
- 空输出给 `ok=None`（中性），不硬说是成功；
- **绝不**在正文里做关键词子串匹配 —— 日志里出现 "error" 太常见，误报的代价是
  用户把已经做对的事重做一遍。

### 3. 事件与持久化（后端）

- `tool_result` 事件新增 `ok` / `exit_code` / `reason` / `duration_ms`
  （duration 从 `_call_event()` 打点的单调时钟算，那是三个发射点的唯一公共入口）；
- `tool_call_logs.status` 允许 `error`（以前无论成败一律 `done`，"哪一步失败了"在库里查不出来）；
- `_merge_tool_log`（回放）带回 `status` / `ok` / `exitCode` / `reason`：
  - `interrupted` **短路**，不走成败判定（中断时写的占位说明是非空文本，交给分类器会得出"成功"）；
  - 老数据 `status='done'` 但结果里是 `Error:` ⇒ 仍标 `error`（否则卡片"已完成 + ↳ Error: …"自相矛盾）；
- `load_history()` 也收尾遗留 `running`（`_reap_stale_if_idle`）：
  **刷新页面**才是用户遇到卡住后的第一动作；但只在"该会话当前没在跑"时才收尾，
  否则会把正在执行的那一步误标成中断。

### 4. 前端

- 右列徽标从"已完成/进行中"扩成 **成功 / 失败（退出码 N）/ 已中断 / 已返回 / 已完成
  / 进行中 · 已 Ns / 生成参数 …**（`成功` 要求每一项都判定为成功；判不出来老实说"已完成"）；
- 卡片下方贴出**结果首行**（`↳ …`）或**失败原因**（`✕ …`），不展开就能看到；
- 新增可展开的**「这一步的具体操作」**：每次调用的 操作（参数/命令）+ 结果 + 单项成败；
- `runLiveness()`：按**距最近一次后端事件的静默时长**给出三级提示
  （<20s 不提示 / 20~90s 如实报静默 / ≥90s 明确怀疑失联 + 「重新连接」按钮）；
- `finishTurn()`：流结束时把仍 `running` 的**工具调用**也标成 `interrupted`
  （修掉"只收尾委派"的漏项），并在这一轮什么都没说时补一句明确说明。

### 5. 提示词

`_PROMPT_HEAD` 新增两段：
- **执行透明**：每个改动世界的动作之后用一句话说清「做了什么 / 作用对象 / 成功还是失败」，
  一轮结束给操作清单；
- **危险命令红线**：不许按进程名批量杀；要重启服务就按端口定位 PID 再精确结束。

## 四、验证

- 后端全量：`394 passed / 0 failed`（新增 `tests/test_p22_run_visibility.py` 28 例：
  判定矩阵、护栏命中/不误伤、子智能体挂载、回放状态与成败、老数据兜底）。
- 前端：`vite build` 通过；`frontend/_verify/verify_run_visibility.mjs` 全绿
  （截图 `run_visibility_shot.png` / `run_visibility_history_shot.png`）；
  P20 的 `verify_tool_labels.mjs` 同步扩词表后仍全绿。
- 顺带修掉验证脚手架自身的一个假故障：`_auth.mjs::ensureBrowserLoggedIn` 在**同一次求值**里
  "填完立刻点"，按钮还停在 `disabled` 那一帧 ⇒ `click()` 静默无效，表现为"登录没反应"。
  现在分两步（填 → 等 150ms → 点）。
