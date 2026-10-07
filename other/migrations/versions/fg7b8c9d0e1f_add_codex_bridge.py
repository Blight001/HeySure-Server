"""add local Codex bridge catalog and event journal

Revision ID: fg7b8c9d0e1f
Revises: ff6a7b8c9d0e
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op


revision: str = "fg7b8c9d0e1f"
down_revision: Union[str, Sequence[str], None] = "ff6a7b8c9d0e"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "devicepresence",
        sa.Column("codex_models_json", sa.Text(), nullable=False, server_default="[]"),
    )
    op.create_table(
        "codexsession",
        sa.Column("session_id", sa.String(), primary_key=True),
        sa.Column("user_id", sa.Integer(), sa.ForeignKey("user.id", ondelete="CASCADE"), nullable=False),
        sa.Column("device_id", sa.String(), nullable=False),
        sa.Column("ai_config_id", sa.Integer(), nullable=True),
        sa.Column("codex_thread_id", sa.String(), nullable=False, server_default=""),
        sa.Column("model", sa.String(), nullable=False, server_default=""),
        sa.Column("cwd", sa.String(), nullable=False, server_default=""),
        sa.Column("sandbox_policy", sa.String(), nullable=False, server_default=""),
        sa.Column("approval_policy", sa.String(), nullable=False, server_default=""),
        sa.Column("status", sa.String(), nullable=False, server_default="created"),
        sa.Column("created_at", sa.Float(), nullable=False),
        sa.Column("updated_at", sa.Float(), nullable=False),
    )
    op.create_index("ix_codexsession_user_id", "codexsession", ["user_id"])
    op.create_index("ix_codexsession_device_id", "codexsession", ["device_id"])
    op.create_index("ix_codexsession_status", "codexsession", ["status"])
    op.create_index("ix_codexsession_codex_thread_id", "codexsession", ["codex_thread_id"])
    op.create_table(
        "codexevent",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("session_id", sa.String(), nullable=False),
        sa.Column("user_id", sa.Integer(), sa.ForeignKey("user.id", ondelete="CASCADE"), nullable=False),
        sa.Column("event_type", sa.String(), nullable=False, server_default="event"),
        sa.Column("sequence", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("payload_json", sa.Text(), nullable=False, server_default="{}"),
        sa.Column("created_at", sa.Float(), nullable=False),
    )
    for name, cols in (("ix_codexevent_session_id", ["session_id"]), ("ix_codexevent_user_id", ["user_id"]),
                       ("ix_codexevent_event_type", ["event_type"]), ("ix_codexevent_sequence", ["sequence"]),
                       ("ix_codexevent_created_at", ["created_at"])):
        op.create_index(name, "codexevent", cols)


def downgrade() -> None:
    op.drop_table("codexevent")
    op.drop_table("codexsession")
    op.drop_column("devicepresence", "codex_models_json")

