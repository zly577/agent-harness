# P3 虚拟文件系统沙箱与执行路由设计

## 目标

实现"判断路由"：**默认可写文件但不可执行；需要执行代码时才接入沙箱执行后端**。

## 架构

```
create_deep_agent(
    backend=build_main_backend(),   # StoreBackend + InMemoryStore（虚拟文件系统，无 execute）
    checkpointer=..., store=...,    # P2 的 SQLite 持久化
    middleware=[SummarizationMiddleware, ...]
)
```

## 默认：虚拟文件系统（可写、可列目录、不可执行）

- **backend = `StoreBackend` + `InMemoryStore`**
- 模型可用工具：`ls`、`read_file`、`write_file`、`edit_file`、`delete`、`glob`、`grep`
- `execute` **不可用**：StoreBackend 未实现 `SandboxBackendProtocol`，create_deep_agent
  会令 execute 工具返回错误（已实测：模型明确反馈"没有 execute 工具"）。
- 命名空间 `("assistant","filesystem")` 隔离虚拟文件。

## 需要执行代码：由 Harness 授权

- 默认 `execute_allowed() = False`，`build_exec_backend() = None`（不暴露 execute）。
- **安全红线**：deepagents 的 `LocalShellBackend.execute` 是 **unrestricted**
  （官方 THREAT_MODEL 明确"no sandboxing, is not restricted by virtual_mode"），
  因此绝不能作为默认或无条件暴露。
- P8 人工审批 + Harness 授权后，才对指定会话启用受控执行后端（白名单命令 + root_dir）。

## P3 验证所得的关键技术事实

| 问题 | 结论 |
|------|------|
| StoreBackend `awrite/aread` 用 async store，`ls/glob` 用 sync store | 必须用同时支持 async+sync 的 store |
| `AsyncSqliteStore` | 只支持 async → `ls/glob` 报 "Synchronous calls ... detected" |
| 同步 `SqliteStore` | 只支持 sync → `awrite` 报 "does not support async methods" |
| **`InMemoryStore`** | 官方支持 async+sync，**实测全兼容** ← 采用 |
| sqlite 事务嵌套 | 因改用 InMemoryStore 而不再需要处理 |

## 取舍（经用户确认）

- 虚拟文件系统为**内存态**：进程重启后虚拟文件丢失（消息历史仍在 SQLite 不丢）。
- 未来接入 E2B / Docker / 本地目录沙箱，只需替换 `build_main_backend()` 的实现，
  接口不变（可插拔）。

## 在 `backend/app/agent/sandbox.py`

- `build_main_backend()` → 默认 StoreBackend（可写不执行）
- `build_exec_backend()` → 受控执行后端（默认 None，P8 授权后启用）
- `execute_allowed()` → 判断路由总开关

## 待办（后续阶段）

- P8：人工审批系统（interrupt_on）+ 按会话授权 execute。
- 可能的执行后端：受限 subprocess（白名单）、E2B、Docker。
