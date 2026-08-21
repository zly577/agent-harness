# P6 多智能体——子智能体 YAML 注册表 + Task 委派 + 隔离

## 目标

- 主 Agent 通过 deepagents 内建 `task` 工具委派复杂子任务给子智能体。
- 子智能体用 **YAML 注册表** 配置（`configs/subagents/*.yaml`），可增删。
- 子智能体与主 Agent **同一模型**（`model` 省略 → 继承主 Agent 的 DeepSeek）。
- 子智能体运行在 **隔离上下文窗口** + 权限隔离。

## 架构

```
create_deep_agent(
    subagents=load_all_subagents(),      # 读 configs/subagents/*.yaml → SubAgent 列表
    ...
)
主 Agent 获得 task 工具 → 委派给子代理（isolated context window）
```

## YAML 注册表（backend/configs/subagents/*.yaml）

```yaml
name: researcher
description: 研究型子代理...
system_prompt: |
  研究助手职责...
permissions_allow_read: [/]        # 可选：只读白名单
permissions_allow_write: [/notes/]  # 可选：只写该目录
```

`app/agent/subagents.py`:
- `SubAgentConfig`（Pydantic 校验 name/description/system_prompt）
- `load_all_subagents_cfg()`：读所有 YAML，name 唯一性校验
- `build_subagent(cfg)`：→ deepagents `SubAgent`（省略 model 继承主 Agent；permissions 映射为 FilesystemPermission）
- `load_all_subagents()`：→ SubAgent 列表（挂到 create_deep_agent）
- `available_subagents()` / `reload_subagent_cache()`

## 隔离

1. **上下文隔离**：deepagents `task` 工具官方说明 "Launch ... in an isolated context window"，
   子代理看不到主代理中间过程/工具结果，只回最终消息。
2. **权限隔离**：子代理可配 `permissions_allow_*`（FilesystemPermission），
   限制只能读/写指定路径；`execute` 延续 P3 默认禁用。
3. **同一模型**：不传 model → 继承主 Agent DeepSeek。

## 验证结果

- 模型端到端：主 Agent 主动 `task("researcher", ...)` 委派"调研 RAG"，子代理返回完整结构化结论。
- `available_subagents`: ['researcher']。
- pytest：28 passed。

## 待办

- P8：子代理的敏感工具/审批（interrupt_on 下放给子代理）。
- 可扩展：按子代理配置不同工具集、独立 backend 命名空间。
