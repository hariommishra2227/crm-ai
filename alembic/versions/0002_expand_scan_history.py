"""Expand scan history fields.

Revision ID: 0002
Revises: 0001
"""
from alembic import op
import sqlalchemy as sa

revision = "0002"
down_revision = "0001"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("scan_runs") as batch_op:
        batch_op.add_column(
            sa.Column("alerts_already_open", sa.Integer(), nullable=False, server_default="0")
        )
        batch_op.add_column(
            sa.Column("alerts_updated", sa.Integer(), nullable=False, server_default="0")
        )
        batch_op.add_column(sa.Column("duration_seconds", sa.Float(), nullable=True))
        batch_op.add_column(sa.Column("failure_message", sa.Text(), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table("scan_runs") as batch_op:
        batch_op.drop_column("failure_message")
        batch_op.drop_column("duration_seconds")
        batch_op.drop_column("alerts_updated")
        batch_op.drop_column("alerts_already_open")
