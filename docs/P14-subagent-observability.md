# P14：子智能体"跑得特别久 / 像卡住了"的排查与可观测性改造

> 排查日期：2026-09-17
> 现象：委派子智能体做调研时，界面长时间没有任何变化，"不确定是挂了还是单纯需要很久"
> 复现与证据：[`_probe/subagent_timeline_probe.py`](../_probe/subagent_timeline_probe.py)、
> [`_probe/reasoning_stream_probe.py`](../_probe/reasoning_stream_probe.py)、
> [`_probe/subagent_ids_probe.py`](../_probe/subagent_ids_probe.py)、
> [`_probe/subagent_wiring_probe.py`](../_probe/subagent_wiring_probe.py)
> 浏览器验证：[`frontend/_verify/verify_subagent_ui.mjs`](../frontend/_verify/verify_subagent_ui.mjs)

---

## 0. 结论摘要

| # | 问题 | 严重度 | 结论 |
|---|------|--------|------|
| 1 | 子智能体的**正文/推理/用量事件 id 与卡片 id 不一致** | **Critical** | 内容被渲染成"第二张幽灵卡片"：用户看到的那张卡片永远只有标题+摘要、正文空白满 100 秒。已修（`SubagentCallRegistry`）。 |
| 2 | `SUBAGENT_TIMEOUT_SECONDS` **从未生效** | **Critical** | 取工具的路径 `nodes["tools"].bound._tools` 在 langgraph 1.x 不存在，函数静默返回 False。真卡住 = 无限等。已修。 |
| 3 | 模型**推理阶段完全不流式** | **High** | 实测 31.8s 的回答里前 **26.9s** 是纯推理，`content` 一个字都没有 → 前端黑屏。已修（`thinking` 事件）。 |
| 4 | 子智能体 token 用量前端**永远为空** | Low | 后端发 `usage(scope=subagent)`，前端等 `subagent_usage`，两边对不上。已修。 |
| 5 | 搜索工具失败**识别不到**，导致反复重试 | Medium | 熔断器只认 `status=error` / `Error:` 前缀，而工具返回的是普通中文提示 → 一次调研里发了 16~20 次搜索。已修（`Error:` 前缀）。 |

**一句话**：不是"慢"，也不全是"挂"——是**过程不可观测** + **一个把内容挂错卡片的 id 错误** + **一个静默失效的安全网**。

---

## 1. 实测基线（修复前后对比）

同一条调研型提问："请委派 researcher 子智能体调研…给出 3 条结论"。

| 指标 | 修复前 | 修复后 |
|------|--------|--------|
| 总耗时 | 66.6s / 82.6s / 110.1s（波动大） | 同量级（任务本身就要这么久） |
| **用户可见的最长静默** | **13.9s** | **5.0s**（且只剩步间缝隙） |
| 静默占比 | 36% | 16% |
| 子智能体事件 | 229 条 | `thinking` 416 + `subagent_thinking` 5964 + `token` 1953 |
| 卡片能否看到正文 | ❌（挂到幽灵卡片） | ✅ 实时增长 |
| 超时保护 | ❌ 未生效 | ✅ `_harness_timed=True`，900s |

子智能体的 token 结构（修复后一轮）：7 个模型步、生成 10,528 tokens，其中**推理 5,974（57%）**。
→ 这就是"感觉特别久"的物理原因：**一半以上的生成量是看不见的推理**。

---

## 2. 根因 1（Critical）：id 不一致 → 幽灵卡片

### 现象

浏览器逐秒采样（修复前），第一张卡片：

```
idx | cards | 摘要字符 | cardTextLen | streamLen | 推理块
  6 |   2   |   42    |     77      |     0     |  -
 40 |   2   |   42    |     78      |     0     |  -
 72 |   2   |   42    |     77      |     0     |  -
 73 |   1   |    5    |   5742      |   5575    |  Y     ← 第一张被归档，露出第二张
 77 |   0   |    0    |             |     0     |  -
```

有一张卡片**整整 70 秒只有 77 个字符**（标题 + 一行摘要），而页面同时存在**两张**卡片。

### 根因

langgraph 给子图分配的命名空间是 `tools:<PregelTask 的 uuid>`，**那个 uuid 与 `task` 工具的 `tool_call_id` 毫无关系**：

```
subagent_start / subagent_end / tool_result   → id = call_00_7fJG0yxB3ZqZrdOP
subagent_token / subagent_thinking / subagent_tool / subagent_usage
                                             → id = 0d812bb3-6f62-f3bb-e55d-…
```

前端按 id 归位，于是：

- 卡片按 `call_00_…` 建出来 → 只在 `subagent_start` 时拿到**摘要**；
- 正文/推理/工具/用量全落到 `0d812bb3…` → `ensureDelegation` 又建了**第二张卡片**；
- 第二张卡片没有摘要、没有类型（因为它没见过 `subagent_start`）；
- 用户盯着的是**第一张**——它永远"工作中"且空白。**这正是"像卡住了"的来源。**

更坏的是收尾时：`tool_result` 按 `call_00_…` 把第一张归档，第二张要等 `send()` 的兜底清理才被标成 done → 工具列表里**多出一条空委派记录**（`.tool-subagent ×2`）。

### 修法

`subagent_stream.SubagentCallRegistry`：子图首个事件到达时，把它**认领**到当时尚未被认领的 task 调用 id 上（task 调用按发出顺序排队，子图也按同样顺序开始流式，FIFO 成立），此后所有 `subagent_*` 事件统一改写成该 id。

前端同时加了一层兜底（`ensureDelegation(..., adoptEmpty)`）：**`subagent_start` 之外的**事件若 id 认不出来，宁可认领一张"已有摘要、还没有正文"的卡片，也不再凭空建幽灵卡片。
（`subagent_start` 明令不许认领 —— 它带的是权威 id，必须独立建卡。）

> 为什么不做成"前端按 subagent_type 猜"：两个同类型并发委派时必然错配，比幽灵卡片更难查。

---

## 3. 根因 2（Critical）：超时是摆设

```python
# 旧实现
bound = getattr(nodes.get("tools"), "bound", None)
tool_list = getattr(bound, "_tools", None)      # ← 实测 None
if not tool_list:
    return False                                # ← 静默返回
```

实测 `agent.nodes["tools"].bound` 是 `ToolNode`，工具在 `_tools_by_name`（dict）里，**没有 `_tools`**。
所以 `SUBAGENT_TIMEOUT_SECONDS=900` 从来没生效过，函数只在日志里留了一句 warning。

`_probe/graph_shape_probe.py` 修复前的输出：

```
tools        PregelNode    has_bound=True bound._tools=None
全图搜 task 工具:  共命中 0 个
```

修法：`_iter_bound_tools()` 遍历 langgraph 几代实现里出现过的容器
（`_tools_by_name` / `tools_by_name` / `_tools` / `tools`，dict 与 list 都吃），
并对 `TaskNode` 本体与被 `RunnableBinding` 包一层的情况都覆盖。
返回值语义同时收紧：**True = 已带上超时，False = 这轮没有保护**，`runtime.get_agent()` 会对 False 打 warning。

修复后 `_probe/subagent_wiring_probe.py`：

```
task.coroutine = 'atask'
已加超时(_harness_timed) = True
配置 subagent_timeout_seconds = 900
```

---

## 4. 根因 3（High）：推理阶段完全没有流

`_probe/reasoning_stream_probe.py`：

```
总耗时 31.8s
reasoning 字符 10884（首块 @ 0.58s）
content   字符  2064（首块 @ 26.86s）   ← 前 26.9s 正文一个字都没有
additional_kwargs 出现过的键: ['reasoning_content']
```

DeepSeek 推理模型的思考增量走 `additional_kwargs.reasoning_content`，**不经过 `message.content`**。
而 SSE 只转发 `content` → 模型"想"多久，前端就黑屏多久。

修法：新增 `thinking` / `subagent_thinking` 事件，把推理增量也推给前端：

- 前端在助手气泡与委派卡片上各加一个**默认收起的"思考过程"块**，标题上的点会呼吸、字数实时增长；
- 这个增量同时作为**计时器 / tok/s 的数据源** —— 没有它，推理期间速率恒为 0，"实时"无从谈起。

---

## 5. 根因 4（Low）：子智能体用量事件名对不上

后端发 `usage` + `scope=subagent`，前端 `applyUsageEvent` 直接 `return`，
而它期望的 `subagent_usage`（`applySubagentEvent` 里已实现）后端从不发 → 卡片上永远没有 token 数。

修法：后端在子智能体命名空间下改发 `subagent_usage`（带归一后的 `id`）；前端两条路径都接。

---

## 6. 根因 5（Medium）：搜索失败识别不到

`middleware._is_tool_failure()` 只认三种失败形状：`ToolMessage.status=="error"` / `dict.error` / 文本以 `Error:` 开头。
而 `tavily_search` 缺 key / 报错时返回的是"联网搜索不可用：…"这样的**普通中文文本** → 熔断器看不见失败 → 不计数、不冷却 → 模型继续换词重试。

实测一轮调研里子智能体发了 **16~20 次** `tavily_search`（每步 4 个并行 × 4~5 步），白烧好几个模型步。

修法：两处失败返回都加上 `Error:` 前缀（保留原有可读信息），让熔断器能按 `failure_limit_threshold=5` 正常介入。

> 注：当前环境 `TAVILY_API_KEY` 在**系统环境变量**里（不在 `.env`），搜索本身可用（实测 3.06s 返回 3515 字符）。
> 所以这 16 次不是"失败重试"，而是模型自己的并行检索策略 —— 但一旦 key 失效，就会变成真失败重试，所以仍然要修。

---

## 7. 附带修掉的一个数字错误：tok/s 曾算出 1627

浏览器实测（修复前）：`1m19s · 1627.2 tok/s · 127,903 tokens`。

原因：`turnTokens` 累加的是每个模型步的 `total_tokens`，而 `total = input + output`，
每一步都会把上万 token 的上下文重新算一次 input，多步累加 → 12 万这种无意义数字。

修法：只累加每步的 `output_tokens`（真正生成出来的），并单独累计 `outTokens`。
现在的读数是"本轮真正生成了多少 token / 平均每秒生成多少"。

---

## 8. 最终交付的界面行为

| 需求 | 实现 |
|------|------|
| 主 → 子只展示摘要 | 卡片正文位放 `summarize_task(description)`（压成一行）；完整 description 收在默认收起的"完整任务描述"里 |
| 子智能体输出流式展示 | `subagent_token` + `subagent_thinking` 实时追加到卡片；正文按 Markdown 渲染 |
| 完成后并入工具调用列表、可展开 | `tool_result` 到达时把委派推进 `toolCalls`（`kind='subagent'`），卡片消失、内容进工具块的"子智能体任务"归档区，**默认收起**，展开可见任务/工具/思考/过程/回报 |
| 计时器 + 每秒 token | 助手回复**下方**的 `.meter` 行：`⏱ 12.3s · ≈41.2 tok/s · 1,284 tokens`，0.2s 刷一次；流式时左侧小点呼吸 |

---

## 9. 复现与回归

```bash
# 后端（在 backend/ 下）
python -u ../_probe/subagent_timeline_probe.py     # 时间轴 + 静默段归因
python -u ../_probe/subagent_ids_probe.py          # 事件 id 自洽性（幽灵卡片回归）
python -u ../_probe/subagent_wiring_probe.py       # 超时是否真的套上
python -u ../_probe/reasoning_stream_probe.py      # 推理增量是否可流式

# 前端（在 frontend/ 下）
node _verify/check_bindings.mjs                    # 模板绑定 / class 覆盖
node _verify/verify_subagent_ui.mjs 9222 http://127.0.0.1:5174/ _verify
```

`verify_subagent_ui.mjs` 里有一条**专门的幽灵卡片断言**：
任何采样中都不允许出现「有正文/推理、却没有摘要」的卡片。

---

## 10. 还留着的坑（未处理）

1. **`apply_subagent_timeout` 依赖 langgraph 内部结构**。这次修好了，但下次框架改版仍可能静默失效 ——
   现在至少会打 warning，并且 `runtime.get_agent()` 会把返回值当回事。
2. **FIFO 认领在极端并发下可能错位**：两个委派几乎同时启动、且子图首事件到达顺序与调用发出顺序相反时，
   摘要与正文会互换。前端兜底能防"幽灵卡片"，但防不了"张冠李戴"。要彻底解决得让 deepagents
   在子图配置里带上父级 `tool_call_id`（需改框架或包一层自定义 `task` 工具）。
3. **调研类任务的固有成本**：57% 的生成量是推理 token，一轮 60~110s 属正常量级。
   真正该做的是让用户**看得见这段时间在花什么**（本次已完成），而不是把时间压下去。
