"""Persist personal context and execution budgets."""
from alembic import op
import sqlalchemy as sa

revision = "0008_user_preferences"
down_revision = "0007_workspace_directories"
branch_labels = None
depends_on = None


def upgrade():
    if "user_preferences" not in sa.inspect(op.get_bind()).get_table_names():
        op.create_table("user_preferences",
                        sa.Column("user_id", sa.String(32), primary_key=True),
                        sa.Column("values_json", sa.Text(), nullable=False))


def downgrade():
    op.drop_table("user_preferences")
