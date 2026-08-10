"""add workspace-backed chat message attachments

Revision ID: fb2c3d4e5f6a
Revises: fa1b2c3d4e5f
Create Date: 2026-08-11
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op


revision: str = "fb2c3d4e5f6a"
down_revision: Union[str, Sequence[str], None] = "fa1b2c3d4e5f"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "chatmessageattachment",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column(
            "message_id",
            sa.Integer(),
            sa.ForeignKey("chatmessage.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("user_id", sa.Integer(), sa.ForeignKey("user.id"), nullable=False),
        sa.Column("ai_config_id", sa.Integer(), nullable=True),
        sa.Column("file_ref", sa.String(), nullable=False),
        sa.Column("workspace_path", sa.String(), nullable=False),
        sa.Column("file_name", sa.String(), nullable=False),
        sa.Column("mime_type", sa.String(), nullable=False, server_default="application/octet-stream"),
        sa.Column("bytes", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("token", sa.String(), nullable=False),
        sa.Column("created_at", sa.Float(), nullable=False),
    )
    for name, columns in (
        ("ix_chatmessageattachment_message_id", ["message_id"]),
        ("ix_chatmessageattachment_user_id", ["user_id"]),
        ("ix_chatmessageattachment_ai_config_id", ["ai_config_id"]),
        ("ix_chatmessageattachment_file_ref", ["file_ref"]),
        ("ix_chatmessageattachment_token", ["token"]),
    ):
        op.create_index(name, "chatmessageattachment", columns)


def downgrade() -> None:
    op.drop_table("chatmessageattachment")
