"""MCP 工具源配置（mcp_servers）

Revision ID: 0003_mcp_servers
Revises: 0002_auth_multitenant
Create Date: 2026-09-18

## 这个 revision 做什么

建一张 `mcp_servers`：让用户能在运行时新增/编辑/删除外部 MCP 工具源，
不用再手写 `backend/configs/mcp/*.yaml` 再重启后端。

## 两处刻意的设计选择

**① `user_id` 不可空。**
与 `sessions.user_id` 相反 —— 那三张老表是为了兼容 87 个存量会话才允许 NULL，
而 `mcp_servers` 是全新表、没有历史包袱。允许 NULL 就等于开一个"全局共享 MCP"
的后门：A 配的能读内网的 server，B 的 agent 里也会挂着它。

**② 只建表，不写数据。**
和 0002 同一个理由：迁移脚本不 import 应用代码，也不预置任何 server。
`backend/configs/mcp/*.yaml` 那份来源继续有效（语义 = 运维预置，对所有人可见），
所以它不需要迁进库里。
"""
from alembic import op
import sqlalchemy as sa

revision = "0003_mcp_servers"
down_revision = "0002_auth_multitenant"
branch_labels = None
depends_on = None

_TABLE_KW = {
    "mysql_engine": "InnoDB",
    "mysql_charset": "utf8mb4",
    "mysql_collate": "utf8mb4_unicode_ci",
}


def _has_table(name: str) -> bool:
    return name in sa.inspect(op.get_bind()).get_table_names()


def upgrade() -> None:
    if _has_table("mcp_servers"):
        print("[0003] mcp_servers 已存在，跳过")
        return

    op.create_table(
        "mcp_servers",
        sa.Column("id", sa.String(32), primary_key=True, comment="MCP server ID（uuid4 hex）"),
        sa.Column("user_id", sa.String(32), nullable=False,
                  comment="所属用户（不可空：不存在全局共享的 MCP）"),
        sa.Column("name", sa.String(128), nullable=False, server_default="",
                  comment="唯一名（同一用户下不重复）"),
        sa.Column("transport", sa.String(16), nullable=False, server_default="streamable_http",
                  comment="streamable_http / sse / stdio / websocket"),
        sa.Column("url", sa.String(1024), nullable=False, server_default="",
                  comment="远程端点（stdio 为空）"),
        sa.Column("headers_encrypted", sa.Text(), nullable=False,
                  comment="Fernet 密文（JSON dict：HTTP 头，可能含密钥）"),
        sa.Column("header_keys", sa.Text(), nullable=False,
                  comment="JSON list[str]：只存头名，列表展示用"),
        sa.Column("command", sa.String(512), nullable=False, server_default="",
                  comment="stdio：启动命令"),
        sa.Column("args_json", sa.Text(), nullable=False,
                  comment="stdio：命令参数（JSON list[str]）"),
        sa.Column("env_encrypted", sa.Text(), nullable=False,
                  comment="Fernet 密文（JSON dict：子进程环境变量，可能含密钥）"),
        sa.Column("cwd", sa.String(512), nullable=False, server_default="",
                  comment="stdio：子进程工作目录"),
        sa.Column("enabled", sa.Boolean(), nullable=False, server_default=sa.text("1"),
                  comment="停用后不加载"),
        sa.Column("created_at", sa.DateTime(), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(), nullable=False, server_default=sa.func.now()),
        **_TABLE_KW,
    )
    op.create_index("ix_mcp_servers_user_id", "mcp_servers", ["user_id"])
    print("[0003] 新建 mcp_servers 表")


def downgrade() -> None:
    """回滚：整表删掉。

    表里有加密的 headers/env —— 删了就得重新填一遍。真要回滚请先 mysqldump。
    """
    if not _has_table("mcp_servers"):
        return
    try:
        op.drop_index("ix_mcp_servers_user_id", table_name="mcp_servers")
    except Exception:  # noqa: BLE001 索引名在不同库上可能不一致，删表才是目的
        pass
    op.drop_table("mcp_servers")
    print("[0003] 已删除 mcp_servers 表")
