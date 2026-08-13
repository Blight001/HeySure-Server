"""add revocable user authentication version

Revision ID: c29d0e1f2a3b
Revises: ff6a7b8c9d0e
Create Date: 2026-08-13
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op


revision: str = "c29d0e1f2a3b"
down_revision: Union[str, Sequence[str], None] = "ff6a7b8c9d0e"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "user",
        sa.Column("auth_version", sa.Integer(), nullable=False, server_default="1"),
    )


def downgrade() -> None:
    op.drop_column("user", "auth_version")
