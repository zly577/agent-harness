# 上下文管理

适配 DeepAgents 0.7.1 / LangChain 1.3.14。开发与测试环境：conda `agent_env`。

## 1. 增强摘要与原始历史

`EnhancedSummarizationMiddleware` 扩展 DeepAgents 的公开摘要类，使用其历史落盘、文件引用、旧工具参数裁剪和标准 `ContextOverflowError` 后压缩重试机制。`ConversationArchiveMiddleware` 独立保存展示原文。为兼容已有 checkpoint，保留 `SummarizationMiddleware.before_agent/before_model/after_model/after_agent` 节点。

压缩更新 `_summarization_event`，模型输入使用“摘要 + 近期消息”视图，原始 `messages` 不因正常压缩被删除。历史文件中的媒体由 DeepAgents 保存为引用。旧 LangChain 摘要 checkpoint 仍可读取，归档补齐逻辑保持不变。

## 2. 请求预算

窗口依次取个人/部署的 `context_window_tokens`、模型 profile 的 `max_input_tokens`、`context_fallback_tokens`（默认 32768）。自定义端点建议填写实际部署窗口。

输入预算 = 窗口 - 输出预留 - 安全余量。输出预留取个人设置与模型/本次请求显式输出上限的较大值。默认预留 4096、安全余量 1024，在输入预算的 85% 触发维护。

计数包含消息、系统提示、工具 schema，以及内层中间件将注入的 todo 指引、任务状态、技能索引和工作区记忆。估算采用保守的两字符/token，并根据供应商报告的实际输入用量向上校准；多模态估算依赖 LangChain 规则，不能视作精确 tokenizer 或供应商计费结果。近期保留量还受固定提示占用、校准比例和完整工具事务边界约束。

配置可在个人设置中修改，旧消息条数字段仅保留 API 兼容，不再决定摘要时机。注册子智能体及 general-purpose 子智能体各自使用相同模型与 token 策略的独立实例。

## 3. 分级压缩

1. 单个大文本工具结果或用户消息外置，保留首尾预览和完整文件引用。
2. 接近预算时，把重复文件读取、较大的旧工具结果替换为可恢复的引用。
3. DeepAgents 裁剪旧的文件写入/编辑参数。
4. 仍超阈值时，摘要旧内容并保留近期完整工具事务。
5. 供应商报告标准上下文溢出时，使用框架恢复路径压缩重试。

外置结果按会话和内容哈希存入 backend 的 `context_results/`，不删除调用或结果消息，不改变调用 ID、工具状态及原始 checkpoint。文件写入失败时保留原文。媒体结果保留其类型，不粗暴转成字符串。若固定提示或剩余内容仍超过输入预算，返回可操作错误，不继续发送已知超预算的请求。

## 4. 任务状态

`update_task_state` 将目标、约束、带来源的事实/完成事项、待办、阻塞和产物写入独立 checkpoint 字段。总内容有容量限制，`expected_revision` 防止过期覆盖；同一模型批次只执行一次任务状态更新。

任务状态每轮重新注入。另保留首条及最近五条用户消息的有界原文片段和 ID，截断部分可用 `read_history` 回查。完成事项由模型报告，来源字段是证据引用，不等于运行时自动验证。待审批操作仍以现有 HITL/checkpoint 状态为准。子智能体的任务状态独立，不写入主任务进度。

## 5. 归档召回

`search_history(query, scope, limit)` 返回原始消息/工具内容的片段和 `会话ID/消息ID`；`read_history(message_ids, scope, offset, max_chars)` 按总字符预算分页返回原文。裸消息 ID 只指当前会话。

每次调用重新检查业务数据库：默认仅当前会话；`workspace` 仅包含同一用户、同一工作区的现存会话。猜测 ID、其他用户/工作区、已删除会话都不能通过这些工具读取。工具不接受任意用户或工作区参数。

SQLite 自动补充 `search_text` 列和 FTS5 trigram 索引，支持中文子串；缺少 FTS5 时使用相同权限范围内的字面子串查询。索引只包含可检索文本和工具参数，不索引图片 base64。旧归档按批补齐，部分流式消息更新后同步索引。此迁移在归档连接首次使用时完成，不改动原始 payload，也不需要新增 MySQL 迁移。

## 6. 分层长期记忆

- `Agent.md`：简短核心偏好与约定，默认常驻前 6000 字。兼容现有最多十万字的编辑文件；超出常驻容量时保留磁盘原文并提示回查，不自动重写或删除。
- `.agent-memory/*.md`：详细经验；启动只加载最多 3000 字的文件索引，正文按需读取。
- `remember(key, title, content, source, tier, expected_revision)`：记录来源、UTC 更新时间和当前工作区范围。详细条目默认新建；修改既有文件必须使用当前 revision。核心条目超容量会拒绝写入，并提示改存 detail。
- API 编辑器与记忆工具共用进程内写入锁、原子替换和乐观版本检查。外部编辑器及 `edit_file` 不参与此锁；写入前复查版本用于发现此类并发修改。文件和记忆目录拒绝外部符号链接。

`remember` 只暴露给主智能体，子智能体仍通过其已有文件权限操作工作区，避免记忆工具绕过委派角色的文件限制。

临时任务进度不写入长期记忆。清空核心文件不删除详细条目；可通过工作区文件工具维护、归并或删除详细记忆。

## 观测与验证

checkpoint 的 `context_usage` 保存窗口来源、输入预算、压缩阈值、维护前/发送前估算、供应商实际输入量、校准倍率、累计压缩次数、外置/去重数量和摘要耗时。`duration_ms` 包含下游模型调用；`summary_duration_ms` 只记录摘要调用。摘要实际费用依赖供应商 tracing，本层不推算价格。

测试覆盖重复压缩后的展示与重建、供应商溢出重试、工具事务完整性、外置失败、任务约束恢复、旧归档迁移、中文召回、分页、跨用户/工作区隔离、记忆容量和版本冲突。测试使用隔离数据库、临时文件和离线模型，不调用真实供应商。

在 backend 执行：`conda run -n agent_env python -m pytest -o addopts='' -q`。
