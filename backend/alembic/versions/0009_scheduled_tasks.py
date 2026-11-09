"""Persist scheduled Agent tasks and their execution history."""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.mysql import DATETIME

revision = "0009_scheduled_tasks"
down_revision = "0008_user_preferences"
branch_labels = None
depends_on = None

_UTC_DATETIME = sa.DateTime().with_variant(DATETIME(fsp=6), "mysql", "mariadb")
_TABLE_KW = {
    "mysql_engine": "InnoDB",
    "mysql_charset": "utf8mb4",
    "mysql_collate": "utf8mb4_unicode_ci",
}


def upgrade() -> None:
    inspector = sa.inspect(op.get_bind())
    tables = inspector.get_table_names()
    if "scheduled_tasks" not in tables:
        op.create_table(
            "scheduled_tasks",
            sa.Column("id", sa.String(32), primary_key=True),
            sa.Column("user_id", sa.String(32), nullable=False),
            sa.Column("title", sa.String(128), nullable=False),
            sa.Column("prompt", sa.Text(), nullable=False),
            sa.Column("workspace_id", sa.String(32), nullable=True),
            sa.Column("provider_id", sa.String(32), nullable=True),
            sa.Column("timezone", sa.String(128), nullable=False),
            sa.Column("frequency", sa.String(8), nullable=False),
            sa.Column("time_of_day", sa.Time(), nullable=True),
            sa.Column("weekdays", sa.JSON(), nullable=False),
            sa.Column("once_at", _UTC_DATETIME, nullable=True),
            sa.Column("next_run_at", _UTC_DATETIME, nullable=True),
            sa.Column("enabled", sa.Boolean(), nullable=False),
            sa.Column("created_at", _UTC_DATETIME, nullable=False),
            sa.Column("updated_at", _UTC_DATETIME, nullable=False),
            **_TABLE_KW,
        )

    if "scheduled_runs" not in tables:
        op.create_table(
            "scheduled_runs",
            sa.Column("id", sa.String(32), primary_key=True),
            sa.Column("task_id", sa.String(32), sa.ForeignKey("scheduled_tasks.id", ondelete="CASCADE"), nullable=False),
            # Legacy user/session tables may use different collations. The API
            # validates ownership and clears the session ID on either delete path.
            sa.Column("session_id", sa.String(32), nullable=True),
            sa.Column("trigger_type", sa.String(12), nullable=False),
            sa.Column("scheduled_for", _UTC_DATETIME, nullable=True),
            sa.Column("status", sa.String(24), nullable=False),
            sa.Column("reason", sa.Text(), nullable=True),
            sa.Column("created_at", _UTC_DATETIME, nullable=False),
            sa.Column("started_at", _UTC_DATETIME, nullable=True),
            sa.Column("finished_at", _UTC_DATETIME, nullable=True),
            sa.UniqueConstraint("task_id", "scheduled_for", name="uq_scheduled_runs_occurrence"),
            sa.UniqueConstraint("session_id", name="uq_scheduled_runs_session"),
            **_TABLE_KW,
        )

    # MySQL commits DDL separately, so a retry also completes any missing indexes.
    inspector.clear_cache()
    indexes = {
        "scheduled_tasks": {
            "ix_scheduled_tasks_user_id": ["user_id"],
            "ix_scheduled_tasks_workspace_id": ["workspace_id"],
            "ix_scheduled_tasks_due": ["enabled", "next_run_at"],
        },
        "scheduled_runs": {
            "ix_scheduled_runs_task_created": ["task_id", "created_at"],
            "ix_scheduled_runs_status_created": ["status", "created_at"],
        },
    }
    for table, table_indexes in indexes.items():
        existing = {index["name"] for index in inspector.get_indexes(table)}
        for name, columns in table_indexes.items():
            if name not in existing:
                op.create_index(name, table, columns)


def downgrade() -> None:
    op.drop_table("scheduled_runs")
    op.drop_table("scheduled_tasks")
