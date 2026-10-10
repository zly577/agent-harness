# P16 · 点"同意/拒绝"没反应：多中断恢复

> 现象：用户提了一个 LeetCode Hot 100 的问题（"整理成一份 md 文档"），
> 一轮跑到审批弹窗后**整个会话卡住**——点「同意」和「拒绝」都没有任何反应，
> 弹窗关掉又立刻弹回来。

## 结论先行

不是前端问题，也不是"卡死"。是**恢复请求被 langgraph 拒绝**：

```
RuntimeError: When there are multiple pending interrupts, you must specify
the interrupt id when resuming.
```

这一轮的 checkpoint 里**同时挂着 5 个中断**，而旧实现只读第一个、并且用扁平的
`Command(resume={"decisions": [...]})` 恢复 —— langgraph 直接抛错。

## 为什么会同时挂 5 个中断

看 checkpoint 里的消息序列，一眼就明白：

```
[00] HumanMessage   '给我找出Leecode的hot100里面的python代码的题目，并且给我写出答案给我整理成一份md文档'
[01] AIMessage      tool_calls=['write_todos']
[02] ToolMessage    'Updated todo list to [拆解 Hot 100 清单为 5 个分片（每片 20 题）...]'
[03] AIMessage      tool_calls=['task', 'task', 'task', 'task', 'task']   ← 一个 superstep 里并行委派 5 个子智能体
```

主智能体把 100 道题拆成 5 片，**并行**委派了 5 个 `task`。每个子智能体内部都要
`write_file` 写自己的那一片（part1.md ~ part5.md），而写文件是受人工审批保护的
操作 —— 于是 5 个写操作各挂一个中断：

```
state.tasks: 5 个（每个各带 1 个中断）
interrupts[0] write_file /workspace/part1.md
interrupts[1] write_file /workspace/part2.md
interrupts[2] write_file /workspace/part3.md
interrupts[3] write_file /workspace/part4.md
interrupts[4] write_file /workspace/part5.md
```

`task` 并行不是异常情况，而是这个系统的常态（提示词就要求拆解 + 并行调研），
所以这个 bug 只要"并行委派的任务里有两个以上写文件"就会复现。

## 为什么表现成"点什么都没反应"

链路是这样断的：

1. 前端点「同意」→ `POST /api/v1/approvals/{sid}`（SSE）。
2. 路由读到 `pending=True`（它只看了 `interrupts[0]`），组了一个**单元素**的扁平决策。
3. `Command(resume={"decisions": [{"type": "approve"}]})` → langgraph 抛 RuntimeError。
4. 路由把异常转成 SSE `error` 事件；`_worker` 一定发 `end`，所以流正常收尾
   （**HTTP 层面一切正常**，这才是最难查的地方：200 + 正常收尾 + 一条 error）。
5. 前端 `decide()`：错误文案不匹配 `/无待审批项|正在处理中/`，于是走"失败分支"——
   把弹窗**放回来**，同时把 `⚠️ 审批失败：...` 写进助手气泡。

于是用户看到的就是：点一下 → 弹窗闪一下就回来 → 什么都没变。那句错误写在
对话气泡里，而气泡通常已经滚出视口了。

顺带一个隐患：`decide()` 里 `deciding.value = false` 写在末尾，`deciding` 又是
按钮的 `:disabled` 条件 —— 中途任何抛出都会让按钮**永久**停在"处理中…"。

## 修法

### 1. 读**全部**中断，带上 id（`app/agent/approvals.py`）

`read_pending_approval` 返回：

```python
{
  "pending": True,
  "actions":    [...],                       # 展平：弹窗逐条展示（现在能看到 5 条）
  "interrupts": [{"id": "<中断 id>", "actions": [...]}],   # 新增：按中断分组、带 id
}
```

`pending` 改成以"中断是否存在"为准：即使某个中断的 value 形状不认识（拿不到
action 明细），它也确实挂着，必须让用户能处理掉。

### 2. 按 interrupt id 组恢复映射（同文件 `build_resume_map`）

```python
{interrupt_id: {"decisions": [{"type": "approve"}]}, ...}
```

langgraph 用"键是否都是 xxh3_128 摘要"来区分"映射形态"与"普通值形态"
（`langgraph/pregel/_loop.py`），而 `Interrupt.id` 正好就是这种摘要。

### 3. 恢复并入运行管理器（`app/api/routes/approvals.py`）

原来恢复走的是一个自建 task + 队列 + future 串行链。现在改成和 `/chat/stream` 一致：

```python
run = run_manager.start(session_id, runtime.resume_input(resume))
async for evt in run_manager.subscribe(session_id, since=len(run.events)):
    yield sse(evt)
```

三个好处：

- **状态诚实**：恢复期间该会话的 run 是 `running`，而不是继续谎报
  `awaiting_approval`。旧写法下，恢复途中用户发一条新消息，`runner.start` 不会拦
  （它只看 `running`），于是两个执行流同时改同一份 checkpoint。
- **可回放**：恢复途中刷新页面能接回来（P15 承诺的"刷新不打断"覆盖到审批恢复）。
- **少一套机制**：future 串行链（`_serialized`/`_chains`）删掉了 —— 串行性由运行
  管理器统一保证；那段"为什么不能用 asyncio.Lock"的注释也随之不需要了。

`subscribe` 的 `replay: bool` 参数换成了 `since: int | None`：审批恢复必须
**不回放**缓冲 —— 那一轮在审批前已经堆了 4636 条事件，全量回放会把整个回合在
前端重复渲染一遍。`since=len(run.events)` 精确表达"只看刚起的这段增量"。

### 4. 前端两处加固（`frontend/src/App.vue`）

- `decide()` 整体包进 `try/finally`，`deciding` 一定复位（否则按钮永久禁用）。
- 审批失败时除了写气泡，还弹一条 toast —— 气泡可能在视口外，这就是"看起来没反应"
  的直接来源。

## 实测证据

### 复现（只读）

```
$ python _probe/dump_interrupts.py <sid>
中断数量: 5
interrupts[0..4] → write_file part1.md ~ part5.md
state.tasks: 5 个（每个 interrupts=1）

$ python _probe/multinterrupt_resume_probe.py <sid>
恢复负载键数 = 5（应为中断数）
旧写法（扁平列表，5 项）实测：← 被拒绝: RuntimeError: When there are multiple
pending interrupts, you must specify the interrupt id when resuming.
```

### 修复后

```
$ node _probe/approve_probe.mjs 8010 <sid> approve
[2] HTTP 200 | type=text/event-stream
[3] 流结束 (82.4s)
[4] 事件 4233 条: {subagent_thinking:3838, subagent_token:318, subagent_tool:45,
                   subagent_usage:19, tool_call:3, tool_result:3, approval:1}
```

- 原来的 5 个中断：part4/part5 的 id 从 pending 列表里消失，part1~3 写出了
  `part1.md`(19.6KB) / `part2.md`(23.6KB) / `part3.md`(22.8KB)，子智能体回报
  "已写入 `/workspace/partN.md`" —— 恢复**真的执行了**。
- 82 秒后出现**新的**审批（还没跑完的子智能体又提了 `t.py` 和 `delete`）——
  说明它是正常的"多轮审批"流程，而不是卡死。

## 一个反直觉的副作用：旧中断不会从清单里消失

修好之后会发现弹窗里**还列着"已经写完的 part1.md"**。一开始我以为是中断记录陈旧，
查下来不是 —— 它确实是 langgraph 眼里的"待处理"，只是这个状态永远不会被清理：

```python
# langgraph/pregel/_loop.py（Command(resume=...) 的处理）
if resume_is_map := (isinstance(resume, dict) and all(is_xxh3_128_hexdigest(k) for k in resume)):
    self.config[CONF][CONFIG_KEY_RESUME_MAP] = resume     # 走 resume map 这条支路
else:
    if len(self._pending_interrupts()) > 1:
        raise RuntimeError("... must specify the interrupt id when resuming")
...
for tid, c, v in map_command(cmd=self.input):
    if not (c == RESUME and resume_is_map):     # ← 用 map 时**不写** per-task RESUME 记录
        writes[tid].append((c, v))
```

而 `_pending_interrupts()` 的定义是"有 INTERRUPT 记录 **且** 没有对应 RESUME 记录的"
（`hanging_interrupts = pending - resumed_interrupt_ids`）。既然 map 形态不写 RESUME，
那些中断就永远算"挂着"，`Snapshot.interrupts` 也就一直报出来。

**所以：只能继续用 map 形态恢复**（这是唯一能处理多中断的方式），并且接受清单会
把历史项一起报出来。代价是**空操作**，不是重复执行 —— 实测证据：

```
修改前  part1.md 11:16:21 / part2.md 11:16:21 / … / part5.md 11:16:21
再批准一轮后  part1.md 11:16:21 / part2.md 11:16:21 / … / part5.md 11:16:21   ← mtime 未变
第二轮事件：tool_call:1, tool_result:1   ← 5 个决策里只有 1 个真的执行
```

（第一次批准跑了 3 个真实工具调用、82 秒；第二轮只有 1 个 —— 其余 4 个决策落在
已完成的 task 上，被 langgraph 忽略。）

弹窗因此加了一句话说明，免得用户看到"同一批操作反复出现"以为卡死。

## 已知边界

- **多轮审批是正常的**：一个任务里子智能体可能反复写文件（实测那轮子智能体还会
  自己写 `t.py`、跑它、改它、删 `tmp/t`），于是反复弹审批。弹窗会把这一批有几个
  操作列全（`N 个操作等待确认`），点一次对这批全部生效。
- **`approval.actions` 是全批同决策**：弹窗只有「同意/拒绝」两个按钮，语义是
  "对这批操作统一决策"。要逐个决策需要改成每条一个按钮 —— 目前不做。
- **恢复期间 run 事件缓冲**由运行管理器按 `KEEP_SECONDS=180` 保留。

## 回归

```bash
# 后端
cd backend && python -m pytest tests/test_p8_approvals.py -q     # 14 passed
# 新增用例集中在：多中断 → 恢复映射、按 action 数给决策、缺 id 跳过、
#                read_pending_approval 返回全部中断、value 形状不认识也 pending

# 探针（对着真实卡住的会话跑）
python _probe/dump_interrupts.py <sid>
python _probe/multinterrupt_resume_probe.py <sid>
node   _probe/approve_probe.mjs 8010 <sid> approve
node   _probe/approve_runner_probe.mjs 8010 <sid>   # 恢复期间 run 必须是 running、重复点击必须 409
```
