"""add durable MCP session context and replay settings

Revision ID: 0a1b2c3d4e5f
Revises: f8a9b0c1d2e3
Create Date: 2026-07-12

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy import inspect


revision: str = "0a1b2c3d4e5f"
down_revision: Union[str, Sequence[str], None] = "f8a9b0c1d2e3"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def _columns(table_name: str) -> set[str]:
    inspector = inspect(op.get_bind())
    if table_name not in inspector.get_table_names():
        return set()
    return {column["name"] for column in inspector.get_columns(table_name)}


def upgrade() -> None:
    user_columns = _columns("user")
    if user_columns:
        with op.batch_alter_table("user", schema=None) as batch_op:
            if "mcp_history_compaction_enabled" not in user_columns:
                batch_op.add_column(sa.Column("mcp_history_compaction_enabled", sa.Boolean(), nullable=False, server_default=sa.true()))
            if "mcp_history_result_max_chars" not in user_columns:
                batch_op.add_column(sa.Column("mcp_history_result_max_chars", sa.Integer(), nullable=False, server_default="100"))
            if "conversation_auto_compress_enabled" not in user_columns:
                batch_op.add_column(sa.Column("conversation_auto_compress_enabled", sa.Boolean(), nullable=False, server_default=sa.true()))

    session_columns = _columns("chatsession")
    if session_columns and "described_tools_json" not in session_columns:
        with op.batch_alter_table("chatsession", schema=None) as batch_op:
            batch_op.add_column(sa.Column("described_tools_json", sa.Text(), nullable=False, server_default=""))


def downgrade() -> None:
    session_columns = _columns("chatsession")
    if "described_tools_json" in session_columns:
        with op.batch_alter_table("chatsession", schema=None) as batch_op:
            batch_op.drop_column("described_tools_json")

    user_columns = _columns("user")
    with op.batch_alter_table("user", schema=None) as batch_op:
        for name in (
            "conversation_auto_compress_enabled",
            "mcp_history_result_max_chars",
            "mcp_history_compaction_enabled",
        ):
            if name in user_columns:
                batch_op.drop_column(name)
