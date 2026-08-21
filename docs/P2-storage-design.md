# P2 上下文管理与持久化设计

> 基于用户确认的最终方案：
> - **SQLite** 做 langgraph checkpoint/store（消息历史 / 会话状态 / 短期记忆）
> - **MySQL 8.0**（root）做业务数据持久化（会话元信息 / 长期记忆 / Skills / 子智能体 / MCP 配置 / 审计）
> - 后续能力强项可再迁移 PostgreSQL（对上层无耦合，因为都走统一接口）

## 一、两个存储各自存什么（核心澄清）

### SQLite（langgraph checkpoint + store）: `backend/data/harness.sqlite`
由 **langgraph 官方 saver/store 直接管理**，用于**短期运行态**（能支撑流式续跑、暂停/恢复、工具记忆）：

| 表 | 存什么 | 用于 |
|----|--------|------|
| `checkpoints`（checkpoint） | **每个会话的消息历史快照**（继承 P1 的对话记录，含 HumanMessage/AIMessage/工具消息）；graph 每个节点的执行节点调用、状态、中断点、条件分支 | 会话跨请求恢复、断点续跑、`create_deep_agent` 的 checkpointer 参数用 |
| `writes` | checkpoint 的待提交增量（节点执行过程中分批写入的部分） | 增量保存，支撑长时间任务 |
| `store`（store 命名空间） | **短期记忆**（可按 (agent_id, session_id) 或命名空间存 Key-Value；如当前任务进度、临时上下文、正在进行的子任务参数） | `store` 参数传入 `SqliteStore`，可跨节点共享 |

一句话：**SQLite = 会话消息历史 + graph 执行态 + 短期记忆**，进程重启不丢，但按会话粒度隔离。

### MySQL（业务数据库）: `agent_harness` 库（SQLAlchemy + 未来 alembic 迁移）
用于**长期业务数据**（跨会话、跨请求、需要 SQL 查询/统计/权限的）：

| 表（规划） | 存什么 | 用于 |
|-----------|--------|------|
| `sessions` | 会话主记录：id、用户/外部标识、标题、状态、创建/更新时间、**关联的 checkpoint_id 或 SQLite 路径指针** | 会话列表页、历史查询、会话归属（对接未来登录） |
| `messages`（可选镜像） | 对话摘要层 / 检索用消息（非全量历史，全量在 SQLite） | 长期检索、报表，避免每会话全量读 SQLite |
| `agent_memories` | **长期记忆**（跨会话的 user 画像、偏好、事实库） | `store` 的长期后端，供 P2 的 memory 机制消费 |
| `skills` | Skills 元数据 + YAML 内容（对应 configs/skills/） | P4 的 Skills 注册表 |
| `subagents` | 子智能体定义 + 权限矩阵（对应 configs/subagents/） | P6 子智能体注册表 |
| `mcp_servers` | MCP server 配置（对应 configs/mcp/） | P7 MCP 管理 |
| `audit_logs` | 运行操作审计 | 全流程可追溯（P5 中间件写） |

一句话：**MySQL = 业务主数据 + 长期记忆 + 配置注册表 + 审计**；SQLite 只管"某次会话的执行/消息运行态"。

### 记忆分层总结
| 类型 | 存储 | 生命周期 | 例子 |
|------|------|---------|------|
| 短期记忆（本次会话运行态） | SQLite store | 会话级，随会话 | 当前任务进度、工具中间结果 |
| 长期记忆（跨会话） | MySQL `agent_memories` | 持久，跨会话 | 用户偏好、长期目标、事实 |
| 消息历史（对话本身） | SQLite checkpoint | 会话级，但持久 | HumanMessage/AIMessage 全量 |

> 技术说明：deepagents 的 `memory` 参数可指定 memory 名称（langchain AgentMemory / BaseStore）。P2 先把 checkpoint/store 用 SQLite 接好、消息历史真正持久化；长期记忆入库与 deepagents memory 机制的结合在 P2 后半段做，避免一次铺太大。

## 二、P2 实施步骤
1. MySQL：建 `agent_harness` 库 + `app/db/session_mysql.py`（SQLAlchemy + PyMySQL）
2. 新增配置：`MYSQL_URL`、`SQLITE_PATH`、上下文截断阈值（等）
3. SQLite checkpoint/store：`app/agent/context.py` 组装 `SqliteSaver`+`SqliteStore`，接入 `create_deep_agent`，用真实 `BaseMessage` 列表取代 P1 二元组
4. 消息截断/摘要：超阈值时用 trim + LLM 摘要
5. `sessions` 表 + 会话创建/查询 API（读写 MySQL）
6. 测试 & 讲解"存什么"
