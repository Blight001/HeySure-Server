"""bump mcp_history_result_max_chars default 100 -> 8000 (full-fidelity replay)

Revision ID: 1a2b3c4d5e6f
Revises: 0a1b2c3d4e5f
Create Date: 2026-07-19

The 100-char cap on replayed MCP tool results was lossy enough to defeat
cross-turn recall (the model saw only the first ~100 chars of an earlier tool
output). Compaction is retained purely as a safety valve for pathologically
large single results; the generous 8000 cap restores mainstream full-fidelity
replay. Truncation stays deterministic, so server-side automatic prefix caching
(DeepSeek/OpenAI/Grok) still hits, and the Anthropic path already caches
explicitly.

Only rows still parked on the OLD default (100) are bumped — a user who
deliberately picked another value keeps it.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy import inspect


revision: str = "1a2b3c4d5e6f"
down_revision: Union[str, Sequence[str], None] = "0a1b2c3d4e5f"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

_OLD_DEFAULT = 100
_NEW_DEFAULT = 8000


def _columns(table_name: str) -> set[str]:
    inspector = inspect(op.get_bind())
    if table_name not in inspector.get_table_names():
        return set()
    return {column["name"] for column in inspector.get_columns(table_name)}


def upgrade() -> None:
    if "mcp_history_result_max_chars" not in _columns("user"):
        return
    # Align the column-level default for any future raw insert.
    with op.batch_alter_table("user", schema=None) as batch_op:
        batch_op.alter_column(
            "mcp_history_result_max_chars",
            existing_type=sa.Integer(),
            server_default=str(_NEW_DEFAULT),
        )
    # Bump existing users still on the old default; leave custom values alone.
    op.execute(
        sa.text(
            'UPDATE "user" SET mcp_history_result_max_chars = :new '
            "WHERE mcp_history_result_max_chars = :old"
        ).bindparams(new=_NEW_DEFAULT, old=_OLD_DEFAULT)
    )


def downgrade() -> None:
    if "mcp_history_result_max_chars" not in _columns("user"):
        return
    with op.batch_alter_table("user", schema=None) as batch_op:
        batch_op.alter_column(
            "mcp_history_result_max_chars",
            existing_type=sa.Integer(),
            server_default=str(_OLD_DEFAULT),
        )
    op.execute(
        sa.text(
            'UPDATE "user" SET mcp_history_result_max_chars = :old '
            "WHERE mcp_history_result_max_chars = :new"
        ).bindparams(new=_NEW_DEFAULT, old=_OLD_DEFAULT)
    )
