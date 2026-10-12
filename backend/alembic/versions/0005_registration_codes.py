"""Persist email registration challenges across workers and restarts."""
from alembic import op
import sqlalchemy as sa

revision = "0005_registration_codes"
down_revision = "0004_intent_labels"
branch_labels = None
depends_on = None


def upgrade():
    if "registration_codes" not in sa.inspect(op.get_bind()).get_table_names():
        op.create_table(
            "registration_codes",
            sa.Column("email", sa.String(255), primary_key=True),
            sa.Column("code_hash", sa.String(255), nullable=False),
            sa.Column("sent_at", sa.DateTime(), nullable=False),
            sa.Column("expires_at", sa.DateTime(), nullable=False),
            sa.Column("attempts", sa.Integer(), nullable=False),
        )


def downgrade():
    op.drop_table("registration_codes")
