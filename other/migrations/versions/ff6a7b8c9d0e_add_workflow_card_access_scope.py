"""add explicit AI access scope to workflow cards

Revision ID: ff6a7b8c9d0e
Revises: fe5f6a7b8c9d
Create Date: 2026-08-12
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op


revision: str = "ff6a7b8c9d0e"
down_revision: Union[str, Sequence[str], None] = "fe5f6a7b8c9d"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "workflowcard",
        sa.Column("access_scope", sa.String(), nullable=False, server_default="all"),
    )
    op.add_column(
        "workflowcard",
        sa.Column("allowed_ai_config_ids_json", sa.String(), nullable=False, server_default="[]"),
    )
    op.create_index("ix_workflowcard_access_scope", "workflowcard", ["access_scope"])
    op.execute(sa.text(
        "UPDATE workflowcard SET access_scope = 'owner' "
        "WHERE tags_json LIKE '%\"ai_owner:%'"
    ))


def downgrade() -> None:
    op.drop_index("ix_workflowcard_access_scope", table_name="workflowcard")
    op.drop_column("workflowcard", "allowed_ai_config_ids_json")
    op.drop_column("workflowcard", "access_scope")
