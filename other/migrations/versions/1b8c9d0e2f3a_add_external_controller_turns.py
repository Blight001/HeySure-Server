"""add reliable external controller conversation turns

Revision ID: 1b8c9d0e2f3a
Revises: 0a7b8c9d0e1f
Create Date: 2026-08-15
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op


revision: str = "1b8c9d0e2f3a"
down_revision: Union[str, Sequence[str], None] = "0a7b8c9d0e1f"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "externalcontrollerturn",
        sa.Column("turn_id", sa.String(), primary_key=True),
        sa.Column("user_id", sa.Integer(), sa.ForeignKey("user.id", ondelete="CASCADE"), nullable=False),
        sa.Column("ai_config_id", sa.Integer(), sa.ForeignKey("assistantaiconfig.id", ondelete="CASCADE"), nullable=False),
        sa.Column("credential_id", sa.Integer(), sa.ForeignKey("externalcontrollercredential.id", ondelete="SET NULL"), nullable=True),
        sa.Column("user_message_id", sa.Integer(), sa.ForeignKey("chatmessage.id", ondelete="CASCADE"), nullable=False),
        sa.Column("assistant_message_id", sa.Integer(), sa.ForeignKey("chatmessage.id", ondelete="SET NULL"), nullable=True),
        sa.Column("session_id", sa.String(), nullable=False),
        sa.Column("session_name", sa.String(), nullable=False, server_default=""),
        sa.Column("ai_kind", sa.String(), nullable=False, server_default="assistant"),
        sa.Column("status", sa.String(), nullable=False, server_default="queued"),
        sa.Column("lease_owner", sa.String(), nullable=False, server_default=""),
        sa.Column("lease_expires_at", sa.Float(), nullable=True),
        sa.Column("attempt", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("error_message", sa.String(), nullable=False, server_default=""),
        sa.Column("created_at", sa.Float(), nullable=False),
        sa.Column("updated_at", sa.Float(), nullable=False),
        sa.Column("started_at", sa.Float(), nullable=True),
        sa.Column("finished_at", sa.Float(), nullable=True),
    )
    for name, columns in (
        ("ix_externalcontrollerturn_user_id", ["user_id"]),
        ("ix_externalcontrollerturn_ai_config_id", ["ai_config_id"]),
        ("ix_externalcontrollerturn_credential_id", ["credential_id"]),
        ("ix_externalcontrollerturn_user_message_id", ["user_message_id"]),
        ("ix_externalcontrollerturn_assistant_message_id", ["assistant_message_id"]),
        ("ix_externalcontrollerturn_session_id", ["session_id"]),
        ("ix_externalcontrollerturn_ai_kind", ["ai_kind"]),
        ("ix_externalcontrollerturn_status", ["status"]),
        ("ix_externalcontrollerturn_lease_expires_at", ["lease_expires_at"]),
        ("ix_externalcontrollerturn_created_at", ["created_at"]),
        ("ix_externalcontrollerturn_updated_at", ["updated_at"]),
    ):
        op.create_index(name, "externalcontrollerturn", columns)


def downgrade() -> None:
    op.drop_table("externalcontrollerturn")
