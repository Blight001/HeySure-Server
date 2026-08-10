"""add external MCP controller mode and execution journal

Revision ID: fa1b2c3d4e5f
Revises: f9a0b1c2d3e4
Create Date: 2026-08-10
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op


revision: str = "fa1b2c3d4e5f"
down_revision: Union[str, Sequence[str], None] = "f9a0b1c2d3e4"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    with op.batch_alter_table("assistantaiconfig") as batch:
        batch.add_column(sa.Column("execution_mode", sa.String(), nullable=False, server_default="internal_model"))
        batch.create_index("ix_assistantaiconfig_execution_mode", ["execution_mode"])

    op.create_table(
        "externalcontrollercredential",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("user_id", sa.Integer(), sa.ForeignKey("user.id", ondelete="CASCADE"), nullable=False),
        sa.Column("ai_config_id", sa.Integer(), sa.ForeignKey("assistantaiconfig.id", ondelete="CASCADE"), nullable=False),
        sa.Column("token_hash", sa.String(), nullable=False, unique=True),
        sa.Column("token_prefix", sa.String(), nullable=False, server_default=""),
        sa.Column("label", sa.String(), nullable=False, server_default="Codex"),
        sa.Column("state", sa.String(), nullable=False, server_default="active"),
        sa.Column("scopes_json", sa.String(), nullable=False, server_default='["context:read","mcp:call","run:write","audit:read"]'),
        sa.Column("created_at", sa.Float(), nullable=False),
        sa.Column("expires_at", sa.Float(), nullable=False),
        sa.Column("last_seen_at", sa.Float(), nullable=True),
        sa.Column("revoked_at", sa.Float(), nullable=True),
    )
    for name, columns in (
        ("ix_externalcontrollercredential_user_id", ["user_id"]),
        ("ix_externalcontrollercredential_ai_config_id", ["ai_config_id"]),
        ("ix_externalcontrollercredential_token_hash", ["token_hash"]),
        ("ix_externalcontrollercredential_state", ["state"]),
        ("ix_externalcontrollercredential_expires_at", ["expires_at"]),
        ("ix_externalcontrollercredential_last_seen_at", ["last_seen_at"]),
    ):
        op.create_index(name, "externalcontrollercredential", columns)

    op.create_table(
        "externalcontrollerrun",
        sa.Column("run_id", sa.String(), primary_key=True),
        sa.Column("user_id", sa.Integer(), sa.ForeignKey("user.id", ondelete="CASCADE"), nullable=False),
        sa.Column("ai_config_id", sa.Integer(), sa.ForeignKey("assistantaiconfig.id", ondelete="CASCADE"), nullable=False),
        sa.Column("credential_id", sa.Integer(), sa.ForeignKey("externalcontrollercredential.id", ondelete="CASCADE"), nullable=False),
        sa.Column("status", sa.String(), nullable=False, server_default="queued"),
        sa.Column("title", sa.String(), nullable=False, server_default=""),
        sa.Column("summary", sa.String(), nullable=False, server_default=""),
        sa.Column("error_message", sa.String(), nullable=False, server_default=""),
        sa.Column("lease_owner", sa.String(), nullable=False, server_default=""),
        sa.Column("lease_expires_at", sa.Float(), nullable=True),
        sa.Column("created_at", sa.Float(), nullable=False),
        sa.Column("updated_at", sa.Float(), nullable=False),
        sa.Column("started_at", sa.Float(), nullable=True),
        sa.Column("finished_at", sa.Float(), nullable=True),
    )
    for name, columns in (
        ("ix_externalcontrollerrun_user_id", ["user_id"]),
        ("ix_externalcontrollerrun_ai_config_id", ["ai_config_id"]),
        ("ix_externalcontrollerrun_credential_id", ["credential_id"]),
        ("ix_externalcontrollerrun_status", ["status"]),
        ("ix_externalcontrollerrun_lease_expires_at", ["lease_expires_at"]),
        ("ix_externalcontrollerrun_created_at", ["created_at"]),
        ("ix_externalcontrollerrun_updated_at", ["updated_at"]),
    ):
        op.create_index(name, "externalcontrollerrun", columns)

    op.create_table(
        "externalcontrollerevent",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("user_id", sa.Integer(), sa.ForeignKey("user.id", ondelete="CASCADE"), nullable=False),
        sa.Column("ai_config_id", sa.Integer(), sa.ForeignKey("assistantaiconfig.id", ondelete="CASCADE"), nullable=False),
        sa.Column("credential_id", sa.Integer(), sa.ForeignKey("externalcontrollercredential.id", ondelete="CASCADE"), nullable=True),
        sa.Column("run_id", sa.String(), sa.ForeignKey("externalcontrollerrun.run_id", ondelete="CASCADE"), nullable=True),
        sa.Column("event_type", sa.String(), nullable=False),
        sa.Column("tool_name", sa.String(), nullable=False, server_default=""),
        sa.Column("status", sa.String(), nullable=False, server_default="ok"),
        sa.Column("result_json", sa.String(), nullable=False, server_default="{}"),
        sa.Column("created_at", sa.Float(), nullable=False),
    )
    for name, columns in (
        ("ix_externalcontrollerevent_user_id", ["user_id"]),
        ("ix_externalcontrollerevent_ai_config_id", ["ai_config_id"]),
        ("ix_externalcontrollerevent_credential_id", ["credential_id"]),
        ("ix_externalcontrollerevent_run_id", ["run_id"]),
        ("ix_externalcontrollerevent_event_type", ["event_type"]),
        ("ix_externalcontrollerevent_tool_name", ["tool_name"]),
        ("ix_externalcontrollerevent_status", ["status"]),
        ("ix_externalcontrollerevent_created_at", ["created_at"]),
    ):
        op.create_index(name, "externalcontrollerevent", columns)


def downgrade() -> None:
    op.drop_table("externalcontrollerevent")
    op.drop_table("externalcontrollerrun")
    op.drop_table("externalcontrollercredential")
    with op.batch_alter_table("assistantaiconfig") as batch:
        batch.drop_index("ix_assistantaiconfig_execution_mode")
        batch.drop_column("execution_mode")
