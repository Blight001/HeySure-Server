"""add mutable workflow card editor layout

Revision ID: 6a3b4c5d7e8f
Revises: 5f2a3b4c6d7e
Create Date: 2026-08-21
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op


revision: str = "6a3b4c5d7e8f"
down_revision: Union[str, Sequence[str], None] = "5f2a3b4c6d7e"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    with op.batch_alter_table("workflowcard") as batch:
        batch.add_column(sa.Column("editor_layout_json", sa.Text(), nullable=False, server_default="{}"))


def downgrade() -> None:
    with op.batch_alter_table("workflowcard") as batch:
        batch.drop_column("editor_layout_json")
