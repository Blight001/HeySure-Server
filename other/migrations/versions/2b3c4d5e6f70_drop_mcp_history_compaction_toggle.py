"""drop the mcp_history_compaction_enabled toggle (compaction is now always-on)

Revision ID: 2b3c4d5e6f70
Revises: 1a2b3c4d5e6f
Create Date: 2026-07-19

The per-user on/off switch is removed: the char cap is now a permanent safety
valve. With the generous default cap, ordinary tool results replay in full and
only a pathologically large single result is shortened, so the "off" state
(no cap at all — a footgun that could blow up the context window) is no longer
offered. Only ``mcp_history_result_max_chars`` remains.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy import inspect


revision: str = "2b3c4d5e6f70"
down_revision: Union[str, Sequence[str], None] = "1a2b3c4d5e6f"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def _columns(table_name: str) -> set[str]:
    inspector = inspect(op.get_bind())
    if table_name not in inspector.get_table_names():
        return set()
    return {column["name"] for column in inspector.get_columns(table_name)}


def upgrade() -> None:
    if "mcp_history_compaction_enabled" in _columns("user"):
        with op.batch_alter_table("user", schema=None) as batch_op:
            batch_op.drop_column("mcp_history_compaction_enabled")


def downgrade() -> None:
    if "mcp_history_compaction_enabled" not in _columns("user"):
        with op.batch_alter_table("user", schema=None) as batch_op:
            batch_op.add_column(
                sa.Column(
                    "mcp_history_compaction_enabled",
                    sa.Boolean(),
                    nullable=False,
                    server_default=sa.true(),
                )
            )
