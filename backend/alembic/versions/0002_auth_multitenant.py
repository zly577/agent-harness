"""P18：认证与多租户 + 供应商配置

Revision ID: 0002_auth_multitenant
Revises: 0001_baseline
Create Date: 2026-09-18

## 这个 revision 做什么

1. 建 `users`（登录用户）
2. 建 `llm_providers`（模型供应商，openai/anthropic 双协议）
3. 给 `sessions` 加 `user_id`（归属）与 `provider_id`（会话选定的模型）
4. 给 `tool_call_logs` / `audit_logs` 加 `user_id`

## 三处刻意的设计选择

**① 只改结构，不写数据。**
"创建引导管理员 + 把存量会话/日志归属到它"**不在这里做**，而是放在应用启动时
（`app/db/bootstrap.py`）。原因：迁移脚本一旦 import 应用代码（密码哈希、配置读取），
就与业务代码耦死 —— 以后改 `security.py` 的签名都可能让这份历史迁移跑不起来。
放在启动时还有个额外好处：**每次启动都会兜底回填**，能自愈"迁移完成、但老进程
又写进了无主行"这种过渡期的烂数据。

**② `user_id` 允许 NULL。**
存量行没有归属，加 `NOT NULL` 在老库上直接执行不过去；更要紧的是回填之后任何
旧代码路径再写入无主行都会让整轮对话 500。查询一律是 `WHERE user_id = <我>`，
NULL 行**谁都匹配不到**（fail-closed：最坏是看不见，绝不会被看见）。
详见 `app/db/models.py` 模块头注释。

**③ 每一步都做存在性判断（幂等）。**
因为本项目的测试路径用 `Base.metadata.create_all()` 建库（那套 schema 已经含新列），
所以很可能出现"库是 create_all 建的、然后才第一次跑 alembic"的情况。
没有这些判断，就会撞 "Duplicate column name" 而卡住。
"""
from alembic import op
import sqlalchemy as sa

revision = "0002_auth_multitenant"
down_revision = "0001_baseline"
branch_labels = None
depends_on = None

_TABLE_KW = {
    "mysql_engine": "InnoDB",
    "mysql_charset": "utf8mb4",
    "mysql_collate": "utf8mb4_unicode_ci",
}


def _inspector():
    return sa.inspect(op.get_bind())


def _has_table(name: str) -> bool:
    return name in _inspector().get_table_names()


def _has_column(table: str, column: str) -> bool:
    if not _has_table(table):
        return False
    return column in {c["name"] for c in _inspector().get_columns(table)}


def _has_index(table: str, index: str) -> bool:
    if not _has_table(table):
        return False
    return index in {i["name"] for i in _inspector().get_indexes(table)}


def _add_column_if_missing(table: str, column: sa.Column, index_name: str | None = None) -> None:
    """加列（+可选普通索引），已存在则跳过。"""
    if not _has_table(table):
        print(f"[0002] {table} 不存在，跳过加列 {column.name}")
        return
    if _has_column(table, column.name):
        print(f"[0002] {table}.{column.name} 已存在，跳过")
    else:
        op.add_column(table, column)
        print(f"[0002] 已加列 {table}.{column.name}")
    if index_name and not _has_index(table, index_name):
        op.create_index(index_name, table, [column.name])
        print(f"[0002] 已建索引 {index_name}")


def upgrade() -> None:
    # ---------- 1. users ----------
    if not _has_table("users"):
        op.create_table(
            "users",
            sa.Column("id", sa.String(32), primary_key=True, comment="用户 ID（uuid4 hex）"),
            sa.Column("email", sa.String(255), nullable=False, comment="登录邮箱"),
            sa.Column("password_hash", sa.String(255), nullable=False,
                      server_default="", comment="argon2id 哈希（绝不存明文）"),
            sa.Column("display_name", sa.String(128), nullable=False,
                      server_default="", comment="展示名"),
            sa.Column("role", sa.String(16), nullable=False,
                      server_default="user", comment="user / admin"),
            sa.Column("is_active", sa.Boolean(), nullable=False,
                      server_default=sa.text("1"), comment="禁用后无法登录"),
            sa.Column("created_at", sa.DateTime(), nullable=False, server_default=sa.func.now()),
            sa.Column("updated_at", sa.DateTime(), nullable=False, server_default=sa.func.now()),
            sa.UniqueConstraint("email", name="uq_users_email"),
            **_TABLE_KW,
        )
        print("[0002] 新建 users 表")
    else:
        print("[0002] users 已存在，跳过")

    # ---------- 2. llm_providers ----------
    if not _has_table("llm_providers"):
        op.create_table(
            "llm_providers",
            sa.Column("id", sa.String(32), primary_key=True, comment="供应商 ID（uuid4 hex）"),
            sa.Column("user_id", sa.String(32), nullable=True, comment="NULL=系统级共享；非空=用户私有"),
            sa.Column("name", sa.String(128), nullable=False, server_default="", comment="展示名"),
            sa.Column("protocol", sa.String(16), nullable=False, server_default="openai",
                      comment="openai / anthropic"),
            sa.Column("base_url", sa.String(512), nullable=True, comment="留空用官方默认地址"),
            sa.Column("api_key_encrypted", sa.Text(), nullable=False, comment="Fernet 密文"),
            sa.Column("api_key_hint", sa.String(32), nullable=False, server_default="",
                      comment="掩码，列表展示用"),
            sa.Column("model", sa.String(128), nullable=False, server_default="", comment="模型名"),
            sa.Column("parameters", sa.Text(), nullable=False, comment="JSON：temperature / timeout 等"),
            sa.Column("is_default", sa.Boolean(), nullable=False, server_default=sa.text("0"),
                      comment="每 owner 至多一个为真"),
            sa.Column("enabled", sa.Boolean(), nullable=False, server_default=sa.text("1"),
                      comment="停用后不参与解析"),
            sa.Column("created_at", sa.DateTime(), nullable=False, server_default=sa.func.now()),
            sa.Column("updated_at", sa.DateTime(), nullable=False, server_default=sa.func.now()),
            **_TABLE_KW,
        )
        op.create_index("ix_llm_providers_user_id", "llm_providers", ["user_id"])
        print("[0002] 新建 llm_providers 表")
    else:
        print("[0002] llm_providers 已存在，跳过")

    # ---------- 3. sessions 归属与模型选择 ----------
    _add_column_if_missing(
        "sessions",
        sa.Column("user_id", sa.String(32), nullable=True, comment="所属用户；NULL=存量待回填"),
        index_name="ix_sessions_user_id",
    )
    _add_column_if_missing(
        "sessions",
        sa.Column("provider_id", sa.String(32), nullable=True,
                  comment="会话绑定的模型供应商；NULL=跟随默认"),
        index_name="ix_sessions_provider_id",
    )

    # ---------- 4. 日志表归属 ----------
    _add_column_if_missing(
        "tool_call_logs",
        sa.Column("user_id", sa.String(32), nullable=True,
                  comment="所属用户（冗余自会话，便于直接按用户查）"),
        index_name="ix_tool_call_logs_user_id",
    )
    _add_column_if_missing(
        "audit_logs",
        sa.Column("user_id", sa.String(32), nullable=True, comment="所属用户"),
        index_name="ix_audit_logs_user_id",
    )


def downgrade() -> None:
    """回滚：删列/删表。

    注意：本 revision **不包含数据操作**（没有回填），所以回滚只丢结构，不丢业务数据。
    但 `llm_providers` / `users` 两张表会被整张删掉 —— 里面有用户口令哈希和
    已加密的供应商密钥，删了就要重新建。真要回滚请先 `mysqldump`。
    """
    for table, column, index in (
        ("audit_logs", "user_id", "ix_audit_logs_user_id"),
        ("tool_call_logs", "user_id", "ix_tool_call_logs_user_id"),
        ("sessions", "provider_id", "ix_sessions_provider_id"),
        ("sessions", "user_id", "ix_sessions_user_id"),
    ):
        if _has_column(table, column):
            if index and _has_index(table, index):
                op.drop_index(index, table_name=table)
            op.drop_column(table, column)
            print(f"[0002] 已删列 {table}.{column}")

    for name in ("llm_providers", "users"):
        if _has_table(name):
            op.drop_table(name)
            print(f"[0002] 已删除 {name} 表")
