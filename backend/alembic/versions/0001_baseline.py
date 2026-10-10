"""baseline：P2/P5 已存在的三张表

Revision ID: 0001_baseline
Revises: -
Create Date: 2026-09-18

## 这个迁移为什么长这样（务必先读）

这是一个**中途引入 alembic** 的项目：`sessions` / `tool_call_logs` / `audit_logs`
三张表在真实库里已经存在（87 / 900+ / 188 行），但库里没有 `alembic_version` 表，
也就是"有数据、没历史"。这带来两个必须处理的问题：

1. **不能用 autogenerate 生成这个 revision**。autogenerate 会读当前模型、对比数据库，
   但它默认把"库里有、metadata 里也有"的表视为…实际上它比较的是"目标 metadata"与
   "数据库现状"，生成出来的会是空 diff（看起来没事）或者反过来 DROP TABLE。
   两种都不能用。所以这里是**手写**的。
2. **必须幂等**：这个脚本可能跑在三种库上 ——
   - 老库（三张表都在）→ 什么都不做，只记版本号；
   - 半新库（只建了一部分）→ 缺哪个建哪个；
   - 全新库（啥都没有）→ 三张表都建。

   判断一律用 `sa.inspect(bind).get_table_names()`，不看 alembic 自己的状态。

## 表结构是"这一版"的快照

这里的列定义刻意**冻结在 P18 之前**（没有 `user_id` / `provider_id`）：
后面 `0002` 再负责加列。这样"全新库"和"老库"会走完全相同的两条 revision，
最终 schema 一致。如果这里直接抄当前模型（含 user_id），全新库建出来的表会带
`user_id`，紧接着 0002 再 ADD COLUMN 就会撞"列已存在"。
"""
from alembic import op
import sqlalchemy as sa

revision = "0001_baseline"
down_revision = None
branch_labels = None
depends_on = None

#: 与 MySQL 8 上实际生成的 DDL 对齐（含引擎/字符集/排序规则）
_TABLE_KW = {
    "mysql_engine": "InnoDB",
    "mysql_charset": "utf8mb4",
    "mysql_collate": "utf8mb4_unicode_ci",
}


def _has_table(name: str) -> bool:
    return name in sa.inspect(op.get_bind()).get_table_names()


def upgrade() -> None:
    if not _has_table("sessions"):
        op.create_table(
            "sessions",
            sa.Column("id", sa.String(32), primary_key=True,
                      comment="会话 ID（=checkpoint thread_id）"),
            sa.Column("title", sa.String(255), nullable=False, comment="会话标题"),
            sa.Column("status", sa.String(16), nullable=False, comment="active/archived"),
            sa.Column("created_at", sa.DateTime(), nullable=False, server_default=sa.func.now()),
            sa.Column("updated_at", sa.DateTime(), nullable=False, server_default=sa.func.now()),
            sa.Column("note", sa.Text(), nullable=False, comment="备注"),
            **_TABLE_KW,
        )
        print("[0001] 新建 sessions 表")
    else:
        print("[0001] sessions 已存在，跳过")

    if not _has_table("tool_call_logs"):
        op.create_table(
            "tool_call_logs",
            sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
            sa.Column("session_id", sa.String(32), nullable=False, comment="会话 ID（=thread_id）"),
            sa.Column("call_id", sa.String(64), nullable=False, comment="工具调用 ID"),
            sa.Column("name", sa.String(128), nullable=False, comment="工具名"),
            sa.Column("kind", sa.String(16), nullable=False,
                      comment="tool=普通工具；task=子智能体委派"),
            sa.Column("args", sa.Text(), nullable=False, comment="调用参数（JSON 字符串）"),
            sa.Column("result", sa.Text(), nullable=False, comment="工具返回结果"),
            sa.Column("status", sa.String(16), nullable=False, comment="running/done/error"),
            sa.Column("created_at", sa.DateTime(), nullable=False, server_default=sa.func.now()),
            **_TABLE_KW,
        )
        op.create_index("ix_tool_call_logs_session_id", "tool_call_logs", ["session_id"])
        print("[0001] 新建 tool_call_logs 表")
    else:
        print("[0001] tool_call_logs 已存在，跳过")

    if not _has_table("audit_logs"):
        op.create_table(
            "audit_logs",
            sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
            sa.Column("thread_id", sa.String(64), nullable=False, comment="会话/thread ID"),
            sa.Column("run_id", sa.String(64), nullable=False, comment="run ID"),
            sa.Column("action", sa.String(32), nullable=False, comment="audit 事件类型"),
            sa.Column("detail", sa.Text(), nullable=False, comment="事件详情（JSON 字符串）"),
            sa.Column("created_at", sa.DateTime(), nullable=False, server_default=sa.func.now()),
            **_TABLE_KW,
        )
        op.create_index("ix_audit_logs_thread_id", "audit_logs", ["thread_id"])
        op.create_index("ix_audit_logs_created_at", "audit_logs", ["created_at"])
        print("[0001] 新建 audit_logs 表")
    else:
        print("[0001] audit_logs 已存在，跳过")


def downgrade() -> None:
    """回滚基线：删掉三张表。

    ⚠️ 这会把**会话元信息与工具流水全部删掉**（消息历史在 SQLite 里，不受影响）。
    正常升级流程里根本不会调到它 —— 只有"引入 alembic 之后反悔、要退回
    无迁移管理状态"时才用。真要用之前请先 mysqldump。
    """
    for name in ("audit_logs", "tool_call_logs", "sessions"):
        if _has_table(name):
            op.drop_table(name)
            print(f"[0001] 已删除 {name} 表")
