"""add encrypted stateful bot connections

Revision ID: a07b8c9d0e1f
Revises: c29d0e1f2a3b
Create Date: 2026-08-13
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op


revision: str = "a07b8c9d0e1f"
down_revision: Union[str, Sequence[str], None] = "c29d0e1f2a3b"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "botconnection",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("user_id", sa.Integer(), nullable=False),
        sa.Column("ai_config_id", sa.Integer(), nullable=False),
        sa.Column("channel", sa.String(), nullable=False),
        sa.Column("provider", sa.String(), nullable=False, server_default=""),
        sa.Column("provider_account_id", sa.String(), nullable=False, server_default=""),
        sa.Column("owner_external_id", sa.String(), nullable=False, server_default=""),
        sa.Column("base_url", sa.String(), nullable=False, server_default=""),
        sa.Column("credentials_encrypted", sa.String(), nullable=False, server_default=""),
        sa.Column("sync_cursor", sa.String(), nullable=False, server_default=""),
        sa.Column("state", sa.String(), nullable=False, server_default="disconnected"),
        sa.Column("last_error_code", sa.String(), nullable=False, server_default=""),
        sa.Column("last_seen_at", sa.Float(), nullable=False, server_default="0"),
        sa.Column("created_at", sa.Float(), nullable=False),
        sa.Column("updated_at", sa.Float(), nullable=False),
        sa.ForeignKeyConstraint(["ai_config_id"], ["assistantaiconfig.id"]),
        sa.ForeignKeyConstraint(["user_id"], ["user.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("channel", "ai_config_id", name="uq_bot_connection_channel_config"),
        sa.UniqueConstraint("channel", "provider_account_id", name="uq_bot_connection_provider_account"),
    )
    op.create_index("ix_botconnection_user_id", "botconnection", ["user_id"])
    op.create_index("ix_botconnection_ai_config_id", "botconnection", ["ai_config_id"])
    op.create_index("ix_botconnection_channel", "botconnection", ["channel"])
    op.create_index("ix_botconnection_provider_account_id", "botconnection", ["provider_account_id"])
    op.create_index("ix_botconnection_state", "botconnection", ["state"])


def downgrade() -> None:
    op.drop_table("botconnection")
