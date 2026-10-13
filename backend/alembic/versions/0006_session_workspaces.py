"""Group sessions into isolated workspaces; NULL preserves legacy files."""
from alembic import op
import sqlalchemy as sa
revision = "0006_session_workspaces"
down_revision = "0005_registration_codes"
branch_labels = None
depends_on = None

def upgrade():
    if "workspace_id" not in {c["name"] for c in sa.inspect(op.get_bind()).get_columns("sessions")}:
        op.add_column("sessions", sa.Column("workspace_id", sa.String(32), nullable=True))
        op.create_index("ix_sessions_workspace_id", "sessions", ["workspace_id"])

def downgrade():
    op.drop_index("ix_sessions_workspace_id", table_name="sessions")
    op.drop_column("sessions", "workspace_id")
