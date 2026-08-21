# P8 人工介入 + 安全边界设计

## 目标

1. **三层 Guardrail**：输入（防越权/恶意指令）、输出（防敏感信息泄露）、步骤（操作前检查）。
2. **人工介入审批流**：敏感文件操作触发 deepagents HITL 中断 → 后端提供审批/拒绝 API → 恢复执行。
3. **create_skill 审批**：Agent 创建技能走草稿态，人工审批后激活（P4 预留，P8 用审批框架承接）。

## 架构

```
用户输入 → 输入Guardrail(check_input)
   ↓
Agent 运行（LangGraph）
   ├─ 步骤Guardrail(check_action)：静态规则
   ├─ deepagents permissions(mode="interrupt")：敏感路径写操作 → HITL 中断
   │     → 暂停，抛出 GraphInterrupt
   │     → 后端审批API：GET 查待审批 / POST 批准|拒绝
   │     → Command(resume=HITLResponse) 恢复
   └─ 输出Guardrail(check_output)
```

## 三层 Guardrail（app/agent/guardrails.py）

| 层 | 函数 | 拦截内容 |
|----|------|---------|
| 输入 | `check_input` | ignore-prompt / jailbreak / rm-rf 等恶意或越权指令模式 |
| 步骤 | `check_action` | 工具调用前检查（如文件非绝对路径） |
| 输出 | `check_output` | API key / 私钥 / 密码等敏感信息外泄 |

全部返回 `GuardrailResult(ok, reason)`。规则集中在模块顶部常量，可扩展。

## 人工介入审批流（app/agent/approvals.py + api/routes/approvals.py）

**关键机制**（经实测验证）：
- `create_deep_agent(permissions=[FilesystemPermission(mode="interrupt", paths=["/system/**",...])])`
  → deepagents 自动调用 `_build_interrupt_on_from_permissions` 生成 interrupt_on，
  自动挂 `HumanInTheLoopMiddleware`。
- 模型调用匹配路径的工具（如 write_file 到 /system/xx）→ 中断暂停，graph 处于 pending。
- `avail read_pending_approval(agent, config)`：从 checkpoint 读待审批 action。
- `decide(agent, config, decisions)`：用 `agent.ainvoke(Command(resume={"decisions": [...]}))` 恢复。

**关键坑（已发现并修复）**：
- 权限路径须用 **glob 通配**（如 `/system/**`），`/system/`（目录结尾）不匹配子孙路径。
- HITLResponse.decisions 与参与中断的 tool_call 一一对应；approve 继续 / reject 跳过。

**审批 API**：
```
GET  /api/v1/approvals/{session_id}   → 查待审批 {pending, actions}
POST /api/v1/approvals/{session_id}   → {"decision":"approve|reject"} 审批并恢复
```

**配置权限**：`sandbox.default_permissions()` 默认对 `/system/**` `/admin/**` `/root/**` 的写操作审批。

## 验证结果

- **端到端审批流跑通**：模型写 `/system/config.txt` → 触发中断 → 读到待审批 action
  → approve → 模型确认"工具返回成功（Updated file /system/config.txt）"。
- 三层 Guardrail 测试通过。
- 全套 48 项测试 13s 通过。

## create_skill 审批（衔接 P4）

- P4：`POST /api/v1/skills/` 已能写草稿 YAML（status=draft）。
- P8：审批后的激活逻辑待与审批框架共用（技能草稿→审批→compile+seed）。
  当前审批框架已通用（approvals 模块），后续可复用于技能/其他敏感操作。

## 文件

- `backend/app/agent/guardrails.py`
- `backend/app/agent/approvals.py`
- `backend/app/api/routes/approvals.py`
- `backend/app/schemas/approval.py`
- `backend/app/agent/sandbox.py`（default_permissions）
- `backend/app/agent/runtime.py`（permissions= 接入）
- `backend/tests/test_p8_guardrails.py`、`test_p8_approvals.py`
