"""add per-AI reasoning effort

Revision ID: 0a7b8c9d0e1f
Revises: a63b9d4e2f71
Create Date: 2026-08-14
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op


revision: str = "0a7b8c9d0e1f"
down_revision: Union[str, Sequence[str], None] = "a63b9d4e2f71"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    with op.batch_alter_table("assistantaiconfig") as batch:
        batch.add_column(
            sa.Column(
                "reasoning_effort",
                sa.String(),
                nullable=False,
                server_default="",
            )
        )


def downgrade() -> None:
    with op.batch_alter_table("assistantaiconfig") as batch:
        batch.drop_column("reasoning_effort")
