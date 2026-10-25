"""add durable asynchronous MCP program runs

Revision ID: h1i2j3k4l5m6
Revises: g0h1i2j3k4l5
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "h1i2j3k4l5m6"
down_revision: Union[str, Sequence[str], None] = "g0h1i2j3k4l5"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "mcpprogramrun",
        sa.Column("id", sa.String(), nullable=False),
        sa.Column("user_id", sa.Integer(), nullable=False),
        sa.Column("ai_config_id", sa.Integer(), nullable=True),
        sa.Column("status", sa.String(), nullable=False, server_default="queued"),
        sa.Column("program_json", sa.Text(), nullable=False),
        sa.Column("input_json", sa.Text(), nullable=False, server_default=""),
        sa.Column("allowed_tools_json", sa.Text(), nullable=False, server_default="[]"),
        sa.Column("session_context_json", sa.Text(), nullable=False, server_default=""),
        sa.Column("output_json", sa.Text(), nullable=True),
        sa.Column("error_json", sa.Text(), nullable=True),
        sa.Column("owner", sa.String(), nullable=False, server_default=""),
        sa.Column("lease_until", sa.Float(), nullable=True),
        sa.Column("deadline_at", sa.Float(), nullable=False),
        sa.Column("cancel_requested", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("created_at", sa.Float(), nullable=False),
        sa.Column("updated_at", sa.Float(), nullable=False),
        sa.Column("started_at", sa.Float(), nullable=True),
        sa.Column("finished_at", sa.Float(), nullable=True),
        sa.ForeignKeyConstraint(["user_id"], ["user.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_mcpprogramrun_user_id", "mcpprogramrun", ["user_id"])
    op.create_index("ix_mcpprogramrun_ai_config_id", "mcpprogramrun", ["ai_config_id"])
    op.create_index("ix_mcpprogramrun_status", "mcpprogramrun", ["status"])
    op.create_index("ix_mcpprogramrun_cancel_requested", "mcpprogramrun", ["cancel_requested"])
    op.create_index("ix_mcpprogramrun_created_at", "mcpprogramrun", ["created_at"])
    op.create_index("ix_mcpprogramrun_updated_at", "mcpprogramrun", ["updated_at"])
    op.create_index("ix_mcpprogramrun_owner", "mcpprogramrun", ["owner"])
    op.create_index("ix_mcpprogramrun_lease_until", "mcpprogramrun", ["lease_until"])
    op.create_index("ix_mcpprogramrun_deadline_at", "mcpprogramrun", ["deadline_at"])
    op.create_index(
        "ix_mcpprogramrun_user_status",
        "mcpprogramrun",
        ["user_id", "status"],
    )
    op.create_index(
        "ix_mcpprogramrun_status_lease",
        "mcpprogramrun",
        ["status", "lease_until"],
    )


def downgrade() -> None:
    op.drop_index("ix_mcpprogramrun_status_lease", table_name="mcpprogramrun")
    op.drop_index("ix_mcpprogramrun_user_status", table_name="mcpprogramrun")
    for name in (
        "ix_mcpprogramrun_deadline_at",
        "ix_mcpprogramrun_lease_until",
        "ix_mcpprogramrun_owner",
        "ix_mcpprogramrun_updated_at",
        "ix_mcpprogramrun_created_at",
        "ix_mcpprogramrun_cancel_requested",
        "ix_mcpprogramrun_status",
        "ix_mcpprogramrun_ai_config_id",
        "ix_mcpprogramrun_user_id",
    ):
        op.drop_index(name, table_name="mcpprogramrun")
    op.drop_table("mcpprogramrun")
