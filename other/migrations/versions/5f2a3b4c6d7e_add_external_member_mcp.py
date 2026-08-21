"""add external member MCP sharing

Revision ID: 5f2a3b4c6d7e
Revises: 4e1f2a3b5c6d
Create Date: 2026-08-21
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op


revision: str = "5f2a3b4c6d7e"
down_revision: Union[str, Sequence[str], None] = "4e1f2a3b5c6d"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    with op.batch_alter_table("assistantaiconfig") as batch:
        batch.add_column(sa.Column(
            "external_mcp_enabled",
            sa.Boolean(),
            nullable=False,
            server_default=sa.false(),
        ))
        batch.add_column(sa.Column("external_mcp_public_id", sa.String(32), nullable=True))
    op.execute(sa.text(
        "UPDATE assistantaiconfig "
        "SET external_mcp_public_id = md5(id::text || ':' || random()::text || ':' || clock_timestamp()::text) "
        "WHERE external_mcp_public_id IS NULL"
    ))
    with op.batch_alter_table("assistantaiconfig") as batch:
        batch.alter_column("external_mcp_public_id", existing_type=sa.String(32), nullable=False)
        batch.create_index(
            "ix_assistantaiconfig_external_mcp_enabled",
            ["external_mcp_enabled"],
        )
        batch.create_index(
            "ix_assistantaiconfig_external_mcp_public_id",
            ["external_mcp_public_id"],
            unique=True,
        )

    op.create_table(
        "externalmcpcredential",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("user_id", sa.Integer(), sa.ForeignKey("user.id", ondelete="CASCADE"), nullable=False),
        sa.Column("ai_config_id", sa.Integer(), sa.ForeignKey("assistantaiconfig.id", ondelete="CASCADE"), nullable=False),
        sa.Column("token_hash", sa.String(64), nullable=False),
        sa.Column("token_prefix", sa.String(16), nullable=False, server_default=""),
        sa.Column("label", sa.String(80), nullable=False, server_default="External AI"),
        sa.Column("created_at", sa.Float(), nullable=False),
        sa.Column("expires_at", sa.Float(), nullable=True),
        sa.Column("last_used_at", sa.Float(), nullable=True),
        sa.Column("revoked_at", sa.Float(), nullable=True),
    )
    _create_indexes("externalmcpcredential", (
        ("user_id", False),
        ("ai_config_id", False),
        ("token_hash", True),
        ("created_at", False),
        ("expires_at", False),
        ("last_used_at", False),
        ("revoked_at", False),
    ))

    op.create_table(
        "externalmcpcallaudit",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("user_id", sa.Integer(), sa.ForeignKey("user.id", ondelete="CASCADE"), nullable=False),
        sa.Column("ai_config_id", sa.Integer(), sa.ForeignKey("assistantaiconfig.id", ondelete="CASCADE"), nullable=False),
        sa.Column("credential_id", sa.Integer(), sa.ForeignKey("externalmcpcredential.id", ondelete="SET NULL"), nullable=True),
        sa.Column("protocol_method", sa.String(40), nullable=False, server_default=""),
        sa.Column("tool_name", sa.String(255), nullable=False, server_default=""),
        sa.Column("success", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("error_code", sa.String(80), nullable=False, server_default=""),
        sa.Column("duration_ms", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("created_at", sa.Float(), nullable=False),
    )
    _create_indexes("externalmcpcallaudit", (
        ("user_id", False),
        ("ai_config_id", False),
        ("credential_id", False),
        ("protocol_method", False),
        ("tool_name", False),
        ("success", False),
        ("created_at", False),
    ))


def _create_indexes(table: str, columns: tuple[tuple[str, bool], ...]) -> None:
    for column, unique in columns:
        op.create_index(f"ix_{table}_{column}", table, [column], unique=unique)


def downgrade() -> None:
    op.drop_table("externalmcpcallaudit")
    op.drop_table("externalmcpcredential")
    with op.batch_alter_table("assistantaiconfig") as batch:
        batch.drop_index("ix_assistantaiconfig_external_mcp_public_id")
        batch.drop_index("ix_assistantaiconfig_external_mcp_enabled")
        batch.drop_column("external_mcp_public_id")
        batch.drop_column("external_mcp_enabled")
