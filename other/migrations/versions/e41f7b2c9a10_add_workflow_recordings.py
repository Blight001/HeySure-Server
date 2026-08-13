"""add workflow operation recordings

Revision ID: e41f7b2c9a10
Revises: d30e1f2a3b4c
Create Date: 2026-08-13
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op


revision: str = "e41f7b2c9a10"
down_revision: Union[str, Sequence[str], None] = "d30e1f2a3b4c"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "workflowrecording",
        sa.Column("id", sa.String(), nullable=False),
        sa.Column("user_id", sa.Integer(), nullable=False),
        sa.Column("ai_config_id", sa.Integer(), nullable=True),
        sa.Column("name", sa.String(), nullable=False),
        sa.Column("description", sa.String(), nullable=False),
        sa.Column("status", sa.String(), nullable=False),
        sa.Column("default_device_id", sa.String(), nullable=False),
        sa.Column("device_ids_json", sa.String(), nullable=False),
        sa.Column("event_count", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.Float(), nullable=False),
        sa.Column("updated_at", sa.Float(), nullable=False),
        sa.Column("stopped_at", sa.Float(), nullable=True),
        sa.ForeignKeyConstraint(["user_id"], ["user.id"]),
        sa.ForeignKeyConstraint(["ai_config_id"], ["assistantaiconfig.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_workflowrecording_user_id", "workflowrecording", ["user_id"])
    op.create_index("ix_workflowrecording_ai_config_id", "workflowrecording", ["ai_config_id"])
    op.create_index("ix_workflowrecording_status", "workflowrecording", ["status"])
    op.create_index("ix_workflowrecording_default_device_id", "workflowrecording", ["default_device_id"])
    op.create_index("ix_workflowrecording_created_at", "workflowrecording", ["created_at"])
    op.create_index(
        "ix_workflowrecording_owner_status", "workflowrecording", ["user_id", "ai_config_id", "status"]
    )
    op.execute(
        "CREATE UNIQUE INDEX uq_workflowrecording_active_owner "
        "ON workflowrecording (user_id, COALESCE(ai_config_id, 0)) WHERE status = 'active'"
    )
    op.create_table(
        "workflowrecordingevent",
        sa.Column("id", sa.String(), nullable=False),
        sa.Column("recording_id", sa.String(), nullable=False),
        sa.Column("sequence", sa.Integer(), nullable=False),
        sa.Column("tool_name", sa.String(), nullable=False),
        sa.Column("device_id", sa.String(), nullable=False),
        sa.Column("arguments_json", sa.String(), nullable=False),
        sa.Column("result_json", sa.String(), nullable=False),
        sa.Column("success", sa.Boolean(), nullable=False),
        sa.Column("error", sa.String(), nullable=False),
        sa.Column("created_at", sa.Float(), nullable=False),
        sa.ForeignKeyConstraint(["recording_id"], ["workflowrecording.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("recording_id", "sequence", name="uq_workflowrecordingevent_sequence"),
    )
    op.create_index("ix_workflowrecordingevent_recording_id", "workflowrecordingevent", ["recording_id"])
    op.create_index("ix_workflowrecordingevent_sequence", "workflowrecordingevent", ["sequence"])
    op.create_index("ix_workflowrecordingevent_tool_name", "workflowrecordingevent", ["tool_name"])
    op.create_index("ix_workflowrecordingevent_device_id", "workflowrecordingevent", ["device_id"])
    op.create_index("ix_workflowrecordingevent_created_at", "workflowrecordingevent", ["created_at"])


def downgrade() -> None:
    op.drop_table("workflowrecordingevent")
    op.drop_table("workflowrecording")
