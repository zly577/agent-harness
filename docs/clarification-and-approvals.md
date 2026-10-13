# 主动澄清与操作审批

智能体使用 `ask_user(question, options=[])` 获取完成任务所需的用户信息。
目标存在影响执行的歧义、缺少无法自行查明的必要信息时应提问；已知信息和可合理默认的细节不要反复询问。
是否提问由模型根据系统提示词决定，调用工具后暂停由 LangGraph 中断机制保证。

## 用户流程

1. 智能体提出具体问题，任务进入 `awaiting_input`。
2. 前端展示问题、可选答案和自由输入框。用户可以提交回答或取消任务。
3. 回答通过对应的中断 ID 恢复，成为 `ask_user` 的返回值，智能体继续完成任务。
4. 后续敏感操作仍可进入 `awaiting_approval`，单独等待同意／拒绝。

等待期间不持续调用模型。checkpoint 存储问题与执行进度，刷新、切会话或重启后端后，可通过查询接口恢复问题。
提问与其他工具同时生成时，中间件优先保留一个提问，其余操作在得到回答后重新考虑。
已经启动的其他子任务可能完成当前步骤；不要把依赖缺失信息的工作提前并行委派。

## 接口

- `GET /api/v1/approvals/{session_id}`：查询全部待处理事项。`actions` / `interrupts` 为审批，`clarifications` 为问题列表，包含 `id`、`question`、`options`。
- `POST /api/v1/approvals/{session_id}/answer`：`{"interrupt_id":"...","answer":"...","cancelled":false}`，返回 SSE 恢复过程。
- 取消：同一路径提交 `{"interrupt_id":"...","cancelled":true}`，取消当前挂起的澄清任务，不自动审批其他操作。
- `POST /api/v1/approvals/{session_id}`：仍使用 `{"decision":"approve"}` 或 `{"decision":"reject"}`，只作用于审批项。
- SSE `clarification` 事件携带问题列表和待审批项；运行查询返回 `awaiting_input`、`suspended=true`。

所有入口校验会话归属。过期或类型不匹配的中断 ID 返回 409，空答案返回 422。恢复开始后重复提交返回 409。
存在待处理中断时，新聊天消息返回 409，用户需先回答、取消或审批。

## 与 HITL 共存

HITL 开关和完全访问模式只控制操作审批，不关闭主动澄清。
关闭审批仅拒绝审批项，不回答或取消澄清问题。
多中断始终按 `interrupt_id` 定向恢复，已经完成的并行任务不会继续出现在待处理列表。
取消由澄清中间件在下一次模型调用前结束当前图的任务；子智能体的取消结果返回主智能体后，同样由中间件在下一次模型调用前结束任务。

## 实现位置

- `backend/app/agent/clarification.py`：工具、提示规则、执行优先级及取消中间件。
- `backend/app/agent/approvals.py`：中断分类与完成状态过滤。
- `backend/app/api/routes/approvals.py`：归属和输入校验、回答恢复。
- `backend/app/agent/runner.py`：等待状态与后台恢复。
- `frontend/src/App.vue`、`frontend/src/sse.js`：问题表单、断线恢复和提交。
- `backend/tests/test_clarification.py`：真实图、SQLite 重启、审批共存及接口测试。
