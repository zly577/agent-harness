"""Persist directory-backed workspaces, including empty groups."""
from alembic import op
import sqlalchemy as sa
revision = "0007_workspace_directories"
down_revision = "0006_session_workspaces"
branch_labels = None
depends_on = None

def upgrade():
    from app.core.workspace import workspace_for
    bind = op.get_bind()
    if "workspaces" not in sa.inspect(bind).get_table_names():
        op.create_table("workspaces", sa.Column("id", sa.String(32), primary_key=True),
                        sa.Column("user_id", sa.String(32), nullable=False),
                        sa.Column("name", sa.String(255), nullable=False),
                        sa.Column("directory", sa.Text(), nullable=False))
        op.create_index("ix_workspaces_user_id", "workspaces", ["user_id"])
    for row in bind.execute(sa.text("SELECT DISTINCT workspace_id, user_id FROM sessions WHERE workspace_id IS NOT NULL AND user_id IS NOT NULL")):
        if not bind.execute(sa.text("SELECT id FROM workspaces WHERE id=:id"), {"id": row.workspace_id}).first():
            directory = workspace_for(row.user_id) / "workspaces" / row.workspace_id
            bind.execute(sa.text("INSERT INTO workspaces (id,user_id,name,directory) VALUES (:id,:uid,:name,:directory)"),
                         {"id": row.workspace_id, "uid": row.user_id, "name": directory.name, "directory": str(directory)})

def downgrade():
    op.drop_table("workspaces")
