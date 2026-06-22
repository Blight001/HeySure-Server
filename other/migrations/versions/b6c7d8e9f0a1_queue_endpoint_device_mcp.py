"""queue endpoint-device MCP calls per device

Revision ID: b6c7d8e9f0a1
Revises: e4a5b6c7d8e9
Create Date: 2026-06-14 00:00:00.000000
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "b6c7d8e9f0a1"
down_revision: Union[str, Sequence[str], None] = "e4a5b6c7d8e9"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    with op.batch_alter_table("agentdispatchtask") as batch_op:
        batch_op.add_column(sa.Column("args_json", sa.Text(), nullable=True))
        batch_op.add_column(
            sa.Column("suppress_session_message", sa.Boolean(), nullable=False, server_default=sa.false())
        )
        batch_op.create_index("ix_agentdispatchtask_device_id", ["device_id"], unique=False)


def downgrade() -> None:
    with op.batch_alter_table("agentdispatchtask") as batch_op:
        batch_op.drop_index("ix_agentdispatchtask_device_id")
        batch_op.drop_column("suppress_session_message")
        batch_op.drop_column("args_json")
