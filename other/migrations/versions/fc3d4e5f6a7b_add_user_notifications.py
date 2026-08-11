"""add durable first-party user notifications

Revision ID: fc3d4e5f6a7b
Revises: fb2c3d4e5f6a
Create Date: 2026-08-11
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op


revision: str = "fc3d4e5f6a7b"
down_revision: Union[str, Sequence[str], None] = "fb2c3d4e5f6a"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "usernotification",
        sa.Column("id", sa.String(), primary_key=True),
        sa.Column("user_id", sa.Integer(), sa.ForeignKey("user.id"), nullable=False),
        sa.Column("ai_config_id", sa.Integer(), nullable=True),
        sa.Column("kind", sa.String(), nullable=False, server_default="message"),
        sa.Column("title", sa.String(), nullable=False, server_default=""),
        sa.Column("body", sa.String(), nullable=False, server_default=""),
        sa.Column("severity", sa.String(), nullable=False, server_default="info"),
        sa.Column("status", sa.String(), nullable=False, server_default="unread"),
        sa.Column("source", sa.String(), nullable=False, server_default="message.send+to"),
        sa.Column("action_url", sa.String(), nullable=False, server_default=""),
        sa.Column("attachments_json", sa.Text(), nullable=False, server_default="[]"),
        sa.Column("app_push_required", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("external_channel", sa.String(), nullable=False, server_default=""),
        sa.Column("external_delivered", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("created_at", sa.Float(), nullable=False),
        sa.Column("updated_at", sa.Float(), nullable=False),
        sa.Column("read_at", sa.Float(), nullable=True),
    )
    for name, columns in (
        ("ix_usernotification_user_id", ["user_id"]),
        ("ix_usernotification_ai_config_id", ["ai_config_id"]),
        ("ix_usernotification_kind", ["kind"]),
        ("ix_usernotification_severity", ["severity"]),
        ("ix_usernotification_status", ["status"]),
        ("ix_usernotification_app_push_required", ["app_push_required"]),
        ("ix_usernotification_created_at", ["created_at"]),
        ("ix_usernotification_updated_at", ["updated_at"]),
        ("ix_usernotification_read_at", ["read_at"]),
    ):
        op.create_index(name, "usernotification", columns)


def downgrade() -> None:
    op.drop_table("usernotification")
