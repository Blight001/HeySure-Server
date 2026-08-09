"""add assistant AI avatar column

Revision ID: d7e8f9a0b1c2
Revises: c6d7e8f9a0b1
Create Date: 2026-08-09
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op


revision: str = "d7e8f9a0b1c2"
down_revision: Union[str, Sequence[str], None] = "c6d7e8f9a0b1"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def _column_names() -> set[str]:
    inspector = sa.inspect(op.get_bind())
    return {str(column["name"]) for column in inspector.get_columns("assistantaiconfig")}


def upgrade() -> None:
    # Older deployments created this column from runtime startup code. Keep the
    # migration tolerant of that historical schema drift, then let Alembic own
    # the column for all future installations.
    if "avatar" not in _column_names():
        op.add_column("assistantaiconfig", sa.Column("avatar", sa.String(), nullable=True))


def downgrade() -> None:
    if "avatar" in _column_names():
        op.drop_column("assistantaiconfig", "avatar")
