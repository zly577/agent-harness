# P5 中间件框架设计

## 目标

在 deepagents / langchain 的 middleware 机制之上，实现统一的**日志 / 审计 / 限流**管道，
让每次 Agent 运行可观测、可追溯、可限流。

## 架构

```
create_deep_agent(
    middleware = [
        SummarizationMiddleware,   # P2 上下文压缩
        LoggingMiddleware,         # P5 结构化运行日志
        AuditMiddleware,           # P5 审计 → MySQL audit_logs
        RateLimitMiddleware,       # P5 限流（默认关闭）
        SkillsMiddleware,          # P4 技能
    ]
)
```

## 三个中间件

| 中间件 | 用到的 hook | 作用 | 输出 |
|--------|------------|------|------|
| `LoggingMiddleware` | `abefore_agent`/`after_model`/`awrap_tool_call`/`aafter_agent` | 记录模型调用次数、工具调用、耗时 | 结构化日志 |
| `AuditMiddleware` | 同上 + `aafter_agent` | 把每次运行统计写入 MySQL `audit_logs` | MySQL 表 |
| `RateLimitMiddleware` | `abefore_agent`/`abefore_model` | 可选消息数上限，超限报错 | 异常/护栏 |

## AuditMiddleware 写入的字段（audit_logs 表）

- `thread_id`：会话 ID（= langgraph thread_id）
- `run_id`：一次 run 的 ID
- `action`：事件类型（`agent_run` / `test_event` 等）
- `detail`：JSON（messages 数 / model_calls / tool_calls / tools 列表 / elapsed_ms）
- `created_at`：时间

## 实现要点

- create_deep_agent 以异步运行（ainvoke），故中间件实现**异步 hooks**（`aafter_*`/`awrap_*`）。
- hook 自动收集：AgentMiddleware 子类通过方法名匹配暴露的 hooks。
- 审计写入失败不影响主流程（try/except + warn）。
- `state` 读取用 `_safe_messages()` 容错（TypedDict/dict 差异）。

## 验证结果

- 真实对话跑通：`audit_logs` 收到 `agent_run` 记录，detail 含 messages=2、model_calls=1、elapsed_ms 等。
- pytest：22 passed。

## 待办

- P8：限流与审批流结合（interrupt_on），敏感操作审批。
- 可把 audit 与前端（P9）监控台打通。
