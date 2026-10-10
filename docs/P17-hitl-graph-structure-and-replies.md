# P17 · 审批开关的图结构一致性 + "必有最终回复"

> 现象（用户报告，同一轮对话里同时出现）：
> 1. 让它整理 LeetCode Hot 100 题解，**工具都调成功、任务也完成了，但一个字都没回复**。
> 2. 右上角人工介入开关切到"自动执行"之后，**审批窗口照旧弹出来**；
>    点「同意」却提示**已经审批过了**。

两个现象看着无关，根因其实是**同一件事**：中断在 checkpoint 里，而"图里有没有
HITL 节点"决定了同一份 checkpoint 读出来是什么样子。

---

## 一、现象 2：图结构随开关变化 → 同一份 checkpoint 三种自相矛盾的读数

### 机理

deepagents 的审批中间件是**编译期**决定的：`create_deep_agent(permissions=[...])`
里只要有一条 `mode="interrupt"` 的规则，它才挂 `HumanInTheLoopMiddleware`
（`deepagents/graph.py:872-876`）。旧实现"关掉人工介入"= `permissions=[]`，
于是**中间件从图里消失了**。

而 `StateSnapshot.interrupts` 不是从 checkpoint 直接读出来的，是 langgraph 拿
checkpoint 里的**待跑任务**逐个到**当前这份图的 nodes** 里匹配出来的
（`prepare_next_tasks(..., self.nodes, ...)`）。HITL 节点没了，那个中断任务就推不出来：

| 开关 | `state.next` | `state.interrupts` | 用户看到 |
|------|--------------|--------------------|----------|
| 需要审批 | `('HumanInTheLoopMiddleware.after_model',)` | 1 条 | 弹窗（正确） |
| 自动执行 | `()` | 0 条 | **弹窗还在**，但点「同意」→ 409「无待审批项」→ 提示"已审批过" |
| 再切回审批 | `(...)` | **又是那同一条**（id 不变） | 弹窗**复活** |

这就是"自动执行之后还弹审批窗"+"点同意说已审批过"的**全部来源**。
注意第二行的自相矛盾：后端说没有中断（0 条），但前端内存里那个弹窗还在，
用户看不出"它已经无效了"。

### 修法：关闭时**保留**中间件，只让审批表永不命中

```python
# app/agent/permissions.py
DISABLED_INTERRUPT_KEY = "__hitl_disabled__"

def hitl_interrupt_on() -> dict[str, Any]:
    if hitl_enabled(...):
        return default_interrupt_on()          # 真正的敏感路径规则
    return {DISABLED_INTERRUPT_KEY: {...}}     # 永不命中的占位键
```

`HumanInTheLoopMiddleware` 只做 `self.interrupt_on.get(tool_call["name"])` 查表 ——
表里有一个真实工具永远不可能叫的名字，就既让 deepagents 认为"有审批配置"从而挂上
中间件（`_merge_fs_interrupt_on` 在整体为空时返回 `None` = 不挂），又让实际效果
是**全程放行**。

**图结构在开关前后完全一致** ⇒ 中断始终看得见、也始终恢复得动。

### 附带：`state.next` 作为第二判据

统一图结构解决了"看不见"，但还有一类假阳性要处理：**resume map 形态恢复之后，
langgraph 不写 per-task RESUME 记录**（`_loop.py`：
`if not (c == RESUME and resume_is_map)`），已经处理完的中断仍留在
`Snapshot.interrupts` 里。P16 时代靠"弹窗里加一句说明"缓解，这次直接判掉：

```python
if not interrupts or not (getattr(state, "next", None) or ()):
    return {"pending": False}
```

`state.next` 的语义是"还没跑完的节点"。图真的停在审批上时它必然非空，跑到底时为空。
加上这一条，**已完成的旧中断不再被当成待审批**（回归用例
`test_read_pending_approval_ignores_stale_interrupts_when_graph_finished`）。

### 放弃挂起的中断要**显式**做

关掉人工介入时，如果那个会话正挂着中断，不能"假装没看见" —— 那半轮工具调用会一直
悬着，且下次发消息会落在挂起状态上。所以要真收尾：

```python
# app/api/routes/approvals.py
async def clear_pending_interrupt(session_id=None) -> list[str]:
    """把挂起的中断用 reject 唤醒，让本轮正常收尾。"""
```

收尾对象从哪来？前端只知道自己内存里记过弹窗的那几个会话（刷新过、切过页面就漏），
所以后端自己留一份进程内登记表（`approvals._pending_sessions`），只在
`read_pending_approval` 真的读到 pending 时登记。开关切换时由
`hitl.py` 逐个调用收尾，并把结果放进 `HitlConfigResponse.cleared`。

---

## 二、现象 1：任务跑完了却没有任何回复

### 机理（三条叠加）

1. **`approval` 事件原本不带文本**。`agent_event_stream` 走到"有中断"分支时
   只 `yield ("approval", pinfo)` 就 `return`。前端收到 `approval` 只弹窗、不写气泡 ——
   用户看到的就是"工具都成功跑完了，却一个字都没回我"。
2. **前端对 `result` 的处理是"有内容才写进气泡"**。空字符串等于什么都没发生。
3. **重置后的图读不出中断**（见现象 2），于是"有中断 → 弹窗、无中断 → result"
   这条二选一的分支会走到"看起来哪边都没输出"的死角。

### 修法

**后端（`app/agent/runtime.py`）**

```python
if pinfo.get("pending"):
    note = await _final_reply_text(agent, config)   # 新增：把"我已经说到哪儿了"带上
    yield ("approval", {**pinfo, "text": note})
    return

text = await _final_reply_text(agent, config)
if not text.strip():
    text = _empty_reply_fallback(session_id)        # 新增：空回复必须有兜底文案
yield ("result", text)
```

`_empty_reply_fallback()` 从工具流水（`tool_call_logs`）里回收本轮**写过/读过哪些文件**
列成清单，输出"（这一轮结束时没有产出文字回复。）+ 最近写入/修改过的文件"。
宁可说清楚"产物在这些位置"，也不留一个空气泡。

**前端（`frontend/src/App.vue`）**

- 新增 `applyApprovalEvent(target, evt, sid)`：审批事件统一走它 —— 同时设弹窗、
  写 `sessionStates[sid].approval`、把 `evt.text` 写进当前气泡。三处调用点
  （首轮流式 / 恢复 / 后台运行订阅）全部收敛到这一个函数，避免再出现"某一处漏了"。
- `restorePendingApproval(sid)` 改成**以后端为准**：后端说没有 pending，
  就把内存里的弹窗**清掉**（旧实现只在 pending 时设置，从不清理）。

---

## 三、前端一律以后端为准（幽灵弹窗）

旧 `openSession` 有一句 `approval.value = sessionStates[id].approval || null` ——
**把前端内存里的旧弹窗照搬回来**。如果这个中断已经在别处（另一个标签页 /
切开关时的自动收尾）被处理掉了，用户切回这个会话就会看到一个**幽灵弹窗**：
后端明明没有待审批，页面上却有一个按钮点了没用。

改成：

```js
// openSession 里不再照搬内存，统一交 restorePendingApproval 去问后端
messages.splice(0, messages.length, ...list)
restorePendingApproval(id)     // 有 → 弹窗；没有 → 清掉
```

---

## 四、附带发现：`uvicorn --reload` 关不掉（会留下假死进程）

排查时发现用户的 8000 **在监听端口、连接能建立，但完全不应答 HTTP**
（`/health` 超时、CPU 0%）。复现出来是一条独立的坑：

```
WatchFiles detected changes ... Reloading...
（之后再也没有 "Started server process"）   ← reload 卡在半路
```

根因是本项目早就记录过的 **aiosqlite 全局连接导致解释器退出挂起**：
`--reload` 要关掉旧 worker 时，进程退出被卡住，父进程一直等，新 worker 起不来，
端口被父进程占着但没人处理请求。

修法（两处）：

```python
# app/agent/context.py —— 显式释放 aiosqlite 连接
async def close_agent_context() -> None:
    """关闭 checkpointer / store 的底层连接（进程退出前调用）。"""
    global _saver, _store, _conn
    for obj in (_saver, _store):
        ...await obj.aclose()...
    if _conn is not None:
        await _conn.close()

# app/main.py —— lifespan 里收尾
finally:
    from app.agent.context import close_agent_context
    await close_agent_context()
```

验证探针 `_probe/exit_hang_probe.py`：

| | 修复前 | 修复后 |
|---|---|---|
| 脚本退出耗时 | **>30s（被 timeout kill，exit=124）** | 秒级正常退出（exit=0） |

---

## 五、验证

### 1. 机制级复现（隔离临时库，不碰用户数据）

| 探针 | 结论 |
|------|------|
| `_probe/interrupt_state_probe.py <sid>` | 拿到"图结构变化 → 中断隐身/复活"的决定性证据 |
| `_probe/hitl_conflict_probe.py` | 完整复现"自动执行后仍弹窗 + 点同意说已审批过" |
| `_probe/hitl_reopen_probe.py` | 复现"重开人工介入 → 旧中断复活" |
| `_probe/multi_interrupt_approve_probe.py` | 一次审批多个操作（5 个）的收尾行为 |
| `_probe/exit_hang_probe.py` | 解释器退出挂起（reload 假死的根因） |

### 2. 断言式验证

```
$ python _probe/hitl_fix_verify.py
14/14 通过
```

### 3. 单元测试

```
$ cd backend && python -m pytest tests/test_p8_approvals.py -q
24 passed
```

新增用例（本轮）：

- `test_read_pending_approval_ignores_stale_interrupts_when_graph_finished` —— 幽灵中断不再算 pending
- `test_read_pending_approval_still_pending_when_graph_suspended` —— 真停住时仍然 pending
- `test_read_pending_approval_registers_and_forgets_session` —— 登记表行为
- `test_hitl_interrupt_on_keeps_middleware_when_disabled` —— 关掉也保留中间件
- `test_last_assistant_text_skips_tool_call_only_message` / `_prefers_latest_non_empty`
- `test_empty_reply_fallback_is_never_empty` —— 兜底文案恒非空
- `test_sse_event_forwards_approval_text` —— approval 事件带文本
- `test_clear_pending_interrupt_sweeps_registry` / `_handles_errors`

全量后端测试：**128 passed**（无回归）。

### 4. 真实浏览器端到端

`frontend/_verify/verify_hitl_toggle.mjs`（Edge headless + CDP，独立临时库 + 独立 8010）：

```
==== 24/24 通过 ====
```

四个场景，逐条断言：

| 场景 | 关键断言 |
|------|----------|
| 1. 正常点「同意」 | 弹窗消失；后端待审批清空；**这一轮有最终回复**；没有立刻又弹回来 |
| 2. 有挂起中断时切「自动执行」 | 弹窗消失；★ 后端**确实**把挂起中断收尾了（不是"看不见"）；那一轮补上回复；界面**立刻**刷新出该回复（不刷新页面） |
| 3. 再切回「需要审批」 | ★ 旧弹窗**不复活**；后端确认无待审批 |
| 4. 幽灵弹窗 | 别处处理掉后切走再切回，★ 不再出现幽灵弹窗 |
| 5. 控制台 | 无 JS 报错 |

---

## 六、已知边界

- **关闭人工介入后，之前已挂起的中断会被"拒绝"收尾**（不是自动批准）。
  语义上的取舍：能让本轮正常结束、拿到最终回复，代价是那个操作被跳过。
- **`_pending_sessions` 是进程内的**：进程重启后登记表清空。此时若有历史中断挂着，
  前端打开会话仍能通过 `restorePendingApproval` 拿到（它是直接问 langgraph 的），
  只是开关切换时的"全量清扫"扫不到它。
- **审批弹窗仍是全批同决策**（一个「同意」对这批所有操作生效），与 P16 一致。

## 七、回归

```bash
# 后端单测
cd backend && python -m pytest tests/test_p8_approvals.py -q          # 24 passed
cd backend && python -m pytest tests -q                               # 128 passed

# 机制验证（隔离临时库）
cd backend && python ../_probe/hitl_fix_verify.py                     # 14/14

# 退出挂起（reload 假死的根因）
cd backend && timeout 30 python ../_probe/exit_hang_probe.py          # 应秒级 exit=0

# 真实浏览器（需要：8010 后端 + 5174 前端 + 9222 Edge，且要 3 个挂起会话）
node frontend/_verify/verify_hitl_toggle.mjs 9222 http://127.0.0.1:5174/ _verify <sid1> <sid2> <sid3> <sid3关键词>
```

> ⚠️ 验证用的后端必须指向**独立临时库**（`SQLITE_PATH=.../verify_dbs/*.sqlite`），
> 并且用干净环境启动（`env -u PYTHONPATH -u CODEBUDDY_SAFE_DELETE_ENABLED ...`）。
> 不要用第二个进程去写用户正在用的那份 `data/harness.sqlite`（见 MEMORY #14）。
