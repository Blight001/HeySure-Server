"""remove retired maintenance and local Codex integrations

Revision ID: 4e1f2a3b5c6d
Revises: 3d0e1f2a4b5c
Create Date: 2026-08-20
"""

from typing import Sequence, Union

from alembic import op


revision: str = "4e1f2a3b5c6d"
down_revision: Union[str, Sequence[str], None] = "3d0e1f2a4b5c"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # Remove children before parents so PostgreSQL never needs CASCADE here.
    for table in (
        "maintenanceapproval",
        "maintenanceevent",
        "maintenancetask",
        "externalcontrollerturn",
        "externalcontrollerevent",
        "externalcontrollerrun",
        "externalcontrollercredential",
    ):
        op.drop_table(table)
    with op.batch_alter_table("assistantaiconfig") as batch:
        batch.drop_index("ix_assistantaiconfig_execution_mode")
        batch.drop_column("execution_mode")


def downgrade() -> None:
    # Feature data contains credentials, conversations and audit history. A
    # partial reconstruction would be unsafe; rollback is application-forward.
    pass
