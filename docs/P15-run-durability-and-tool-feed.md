# P15：回答不被打断（运行与连接解耦）+ 工具调用的"只展示最新一条"

> 日期：2026-09-17
> 复现：[`_probe/run_manager_probe.py`](../_probe/run_manager_probe.py)、
> [`frontend/_verify/verify_run_persist.mjs`](../frontend/_verify/verify_run_persist.mjs)

---

## 一、回答为什么会被打断（以及怎么修）

### 问题

改造前 `POST /chat/stream` 的 SSE 生成器**就是**驱动 agent 的那个协程：

```python
async def _gen():
    yield sse({"type": "session", ...})
    async for evt in runtime.stream_chat(...):   # ← 这里在跑 agent
        yield sse(evt)
return StreamingResponse(_gen())
```

Starlette 在客户端断开时会**取消**响应任务，于是 `agent.astream(...)` 被取消 ——
这一轮回答直接蒸发。所以刷新页面、关标签页、网络抖一下，回答就没了。

### 修法：运行归运行，连接归连接

新增 `app/agent/runner.py`：

```
POST /chat/stream ──► run_manager.start(...)     ← 后台 task，不属于任何请求
                      run_manager.subscribe(...) ← 这个连接只是"订阅者"之一
```

- **广播**：事件实时推给当前所有订阅者（SSE 连接、以及将来可能的其他消费者）；
- **缓冲**：同一份事件写进可回放的序列，供重连时先回放再接直播。

于是连接断了不影响运行；刷新/重开页面时先回放缓冲，用户看到的是"它一直在跑"。

**只有两种情况会真的停**：`POST /sessions/{id}/stop`（用户明确停止）、进程退出。

### 事件缓冲要合并

一轮调研实测能产生 1.6 万条事件，绝大多数是逐 token 增量。写入缓冲时把**相邻同类增量**
合并成一条（`token` 接 `token`、`subagent_thinking` 接 `subagent_thinking`…）。
合并只发生在缓冲里，**订阅者拿到的仍是细粒度事件**，在线逐字体验不受影响。

实测：一轮 103 条实时事件 → 缓冲里只有 4 条。

### 新增/变更的接口

| 接口 | 作用 |
|------|------|
| `POST /api/v1/chat/stream` | 发消息并订阅这一轮（同一会话已有运行 → 409） |
| `GET  /api/v1/sessions/{id}/run` | 这一轮的状态：`running / awaiting_approval / done / stopped / error` |
| `GET  /api/v1/sessions/{id}/run/stream` | **接回**：回放 + 直播（刷新、切会话、重开页面走这里） |
| `POST /api/v1/sessions/{id}/stop` | 用户明确停止（唯一会中断运行的方式） |
| `GET  /api/v1/sessions` | 每项多一个 `running` 字段 → 侧边栏能显示"那个会话还在输出" |

### 前端配合

- 停止按钮不再 `abort()` 本地请求，改为调 `POST /stop`（否则只是前端不看，后端照跑）；
- `localStorage` 记住"上次在看的会话"，刷新后自动回到原处并 `resumeRun()` 接回；
- `resumeRun()` 会先用后端返回的 `user_message` **丢掉历史里这一轮的残缺助手消息**，
  再靠回放完整重建 —— 否则会出现"半截 + 完整"两份。

### 顺带修掉的一个坑：审批中断被当成"跑完了"

`agent_event_stream` 在**等到人工审批**时也是正常返回的（推一条 `approval` 事件就收尾）。
原实现一律置 `status="done"`，后果是：用户恰好不在页面上（刷新/关标签）时，
回来只会看到一个**空白的助手气泡**，既没有审批弹窗、也接不回这一轮 —— 实际上它还悬着。

修法两处：
1. `runner` 识别"最后一条事件是 approval" → `status = "awaiting_approval"`（不算结束）；
2. 前端 `openSession` 里补 `restorePendingApproval()`：查 `GET /api/v1/approvals/{id}`，
   有待审批就把弹窗恢复出来。

---

## 二、工具调用：同一时刻只展示最新一条

### 问题

原实现是**一整个 `<details>` 绑 `:open`**：

```html
<details class="tools" :open="toolSplit(m).latest?.status === 'running'">
```

于是每来一个新工具，`latest.status` 从 running 翻到 done，整个块就**收起**；
下一个工具又 running，又**展开**……用户看到的就是"工具列表反复开合"。

### 修法：常显的最新条 + 用户自己控制的折叠历史

```
.act            最新一条，永远可见，状态文案实时变（不参与折叠）
.act-history    此前的调用，默认不展开，**开合只由用户决定**
```

只读工具（`ls` / `glob` / `grep` / `read_file`）里**连续出现**的会合并成一条 ——
一次调研里它们会连着来十几条，每条占一行会把正文挤没。

状态**只由右侧徽标说一次**（左列再说一遍就是同一句话说两遍）：

| 列 | 内容 |
|----|------|
| 左列 `.act-verb` | 正在跑 → `正在查看` / `正在写入` / `正在执行命令` / `正在联网搜索`…；**跑完 → 工具本身**（工具名，合并组显示 `ls、glob、read_file`） |
| 中间 `.act-detail` | 操作对象：路径 / 匹配式 / 命令 / 查询词；合并组显示 `N 项 · 最近两个目标` |
| 右列 `.act-badge` | `进行中` / `已完成` —— **唯一说状态的地方** |

> 初版左列在跑完时写的是"已打开/已完成"，实测与右侧徽标重复，已改为显示工具名。

---

## 三、回归

```bash
# 后端（在 backend/ 下）
python -u ../_probe/run_manager_probe.py    # 断开不打断 / 重连回放 / stop 真停
python -u ../_probe/dump_session_msgs.py <session_id>   # 怀疑"回答空了"时 dump 原始消息

# 前端（在 frontend/ 下）
node _verify/check_bindings.mjs
node _verify/verify_tool_labels.mjs 9333 http://127.0.0.1:5174/ _verify   # 工具行文案（不跑模型，秒级）
node _verify/verify_run_persist.mjs 9333 http://127.0.0.1:5174/ _verify   # 工具交互 + 续传 + 停止 + 审批恢复
```

> 校验脚本里一律用 `textContent` 读文本：`innerText` 对滚出视口/未渲染的元素
> 返回空串，历史条目一多就会把"文案没问题"误报成"文案是空的"（踩过）。

`run_manager_probe.py` 实测结论：
订阅 2 秒后主动断开（= 刷新页面）→ 运行照常跑完（`status=done`）→
最终回复完整写入 checkpoint → 重连能回放 → `stop` 后 `status=stopped`。

---

## 四、还留着的边界

1. **同一会话不允许两轮并行**（会同时改同一份 checkpoint）→ 第二次请求返回 409。
   前端在流式中会禁用输入框，正常路径碰不到；刷新后若后端仍在跑，再发消息会收到 409。
2. **审批恢复（`POST /approvals/{id}`）没有走 run_manager**，它自己有一套"独立 task +
   队列"的实现（P8 就有）。所以审批恢复期间刷新，能恢复弹窗、但接不回那段流。
   要统一的话，把 `stream_resume` 也塞进 run_manager 即可。
3. **事件缓冲只驻留内存**：进程重启就没了（checkpoint 里的消息仍在）。
   保留期 `KEEP_SECONDS = 180`，过期清掉以免长期占用内存。
