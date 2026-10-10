"""工具调用意图标签（tool_call_logs 加两列 + 新建 task_steps 读模型）

Revision ID: 0004_intent_labels
Revises: 0003_mcp_servers
Create Date: 2026-09-30

## 这个 revision 做什么

1. `tool_call_logs` 加 `intent_label` / `intent_source`：`tool_call` 事件的
   payload 契约就落在这两列上；
2. 新建 `task_steps`：任务看板「当前步骤」的派生读模型。

## 三处刻意的设计选择

**① 两列都可空，且不给 server_default。**

数据契约写的是 `intent_label: string | null` —— NULL 表达"还没有标签"，
而不是"标签是空字符串"。存量 900+ 条流水在功能上线前就存在，它们天然是
NULL；回填脚本（`scripts/backfill_intent_labels.py`）之后会把它们补上。
如果这里写 `NOT NULL DEFAULT ''`，"没标签"和"标签是空串"就再也分不开了。

**② 不加索引。**

`intent_label` 只用来展示，没有按它筛选的查询。多一个索引就多一份写放大，
而这张表本来就是流式对话里写最频繁的表。

**③ `task_steps` 有 `(session_id, call_id)` 唯一约束。**

这是投影"幂等"的**数据库级**保证：构建器可以放心地重复执行
（`project_session` 连续跑两次，第二次 0 改动），而不依赖应用层的查重逻辑。
没有它，一次并发/重试就会让看板出现两行一模一样的步骤。
"""
from alembic import op
import sqlalchemy as sa

revision = "0004_intent_labels"
down_revision = "0003_mcp_servers"
branch_labels = None
depends_on = None

_TABLE_KW = {
    "mysql_engine": "InnoDB",
    "mysql_charset": "utf8mb4",
    "mysql_collate": "utf8mb4_unicode_ci",
}


def _has_table(name: str) -> bool:
    return name in sa.inspect(op.get_bind()).get_table_names()


def _columns(table: str) -> set[str]:
    if not _has_table(table):
        return set()
    return {c["name"] for c in sa.inspect(op.get_bind()).get_columns(table)}


def upgrade() -> None:
    # ---- 1) tool_call_logs：意图标签两列 ----
    cols = _columns("tool_call_logs")
    if not cols:
        print("[0004] 没有 tool_call_logs 表，跳过加列（全新库由 create_all 负责）")
    else:
        if "intent_label" not in cols:
            op.add_column(
                "tool_call_logs",
                sa.Column("intent_label", sa.String(64), nullable=True,
                          comment=" 意图标签（展示文案，≤24 字符）"),
            )
            print("[0004] tool_call_logs.intent_label 已添加")
        if "intent_source" not in cols:
            op.add_column(
                "tool_call_logs",
                sa.Column("intent_source", sa.String(16), nullable=True,
                          comment=" 标签来源：model/fallback/backfill"),
            )
            print("[0004] tool_call_logs.intent_source 已添加")

    # ---- 2) task_steps：看板读模型 ----
    if _has_table("task_steps"):
        print("[0004] task_steps 已存在，跳过")
        return

    op.create_table(
        "task_steps",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("session_id", sa.String(32), nullable=False, server_default="",
                  comment="会话 ID（=thread_id）"),
        sa.Column("user_id", sa.String(32), nullable=True,
                  comment="所属用户；NULL=存量待回填"),
        sa.Column("call_id", sa.String(64), nullable=False, server_default="",
                  comment="工具调用 ID（幂等键）"),
        sa.Column("step_index", sa.Integer(), nullable=False, server_default="0",
                  comment="会话内顺序（从 1 开始）"),
        sa.Column("tool_name", sa.String(128), nullable=False, server_default="",
                  comment="工具名"),
        sa.Column("display_name", sa.String(128), nullable=False, server_default="",
                  comment="目录里的展示名（未知工具 = 工具名本身）"),
        sa.Column("kind", sa.String(16), nullable=False, server_default="tool",
                  comment="tool / task"),
        sa.Column("status", sa.String(16), nullable=False, server_default="running",
                  comment="running/done/error/interrupted"),
        sa.Column("intent_label", sa.String(64), nullable=True,
                  comment=" 意图标签（与 tool_call 事件同值）"),
        sa.Column("intent_source", sa.String(16), nullable=True,
                  comment=" 标签来源：model/fallback/backfill"),
        sa.Column("created_at", sa.DateTime(), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(), nullable=False, server_default=sa.func.now()),
        sa.UniqueConstraint("session_id", "call_id", name="uq_task_steps_session_call"),
        **_TABLE_KW,
    )
    op.create_index("ix_task_steps_session_id", "task_steps", ["session_id"])
    op.create_index("ix_task_steps_user_id", "task_steps", ["user_id"])
    print("[0004] 新建 task_steps 表")


def downgrade() -> None:
    """回滚：删表 + 删列。意图标签是纯展示数据，删了不丢业务事实。"""
    if _has_table("task_steps"):
        for idx in ("ix_task_steps_session_id", "ix_task_steps_user_id"):
            try:
                op.drop_index(idx, table_name="task_steps")
            except Exception:  # noqa: BLE001 索引名在不同库上可能不一致
                pass
        op.drop_table("task_steps")
        print("[0004] 已删除 task_steps 表")

    cols = _columns("tool_call_logs")
    for name in ("intent_source", "intent_label"):
        if name in cols:
            op.drop_column("tool_call_logs", name)
            print(f"[0004] 已删除 tool_call_logs.{name}")
