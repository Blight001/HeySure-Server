"""add multi-device contracts and workflow AI interactions

Revision ID: f9a0b1c2d3e4
Revises: e8f9a0b1c2d3
Create Date: 2026-08-10
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op


revision: str = "f9a0b1c2d3e4"
down_revision: Union[str, Sequence[str], None] = "e8f9a0b1c2d3"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    with op.batch_alter_table("workflowcardversion") as batch:
        batch.add_column(sa.Column("contract_device_ids_json", sa.String(), nullable=False, server_default="[]"))
    with op.batch_alter_table("workflowconfirmation") as batch:
        batch.add_column(sa.Column("ai_config_id", sa.Integer(), nullable=True))
        batch.add_column(sa.Column("save_as", sa.String(), nullable=False, server_default=""))
        batch.add_column(sa.Column("response_json", sa.String(), nullable=True))
        batch.add_column(sa.Column("notified_at", sa.Float(), nullable=True))
        batch.add_column(sa.Column("notification_run_id", sa.String(), nullable=False, server_default=""))
        batch.create_foreign_key(
            "fk_workflowconfirmation_ai_config_id_assistantaiconfig",
            "assistantaiconfig", ["ai_config_id"], ["id"],
        )
        batch.create_index("ix_workflowconfirmation_ai_config_id", ["ai_config_id"])
        batch.create_index("ix_workflowconfirmation_notification_run_id", ["notification_run_id"])


def downgrade() -> None:
    with op.batch_alter_table("workflowconfirmation") as batch:
        batch.drop_index("ix_workflowconfirmation_notification_run_id")
        batch.drop_index("ix_workflowconfirmation_ai_config_id")
        batch.drop_constraint("fk_workflowconfirmation_ai_config_id_assistantaiconfig", type_="foreignkey")
        batch.drop_column("notification_run_id")
        batch.drop_column("notified_at")
        batch.drop_column("response_json")
        batch.drop_column("save_as")
        batch.drop_column("ai_config_id")
    with op.batch_alter_table("workflowcardversion") as batch:
        batch.drop_column("contract_device_ids_json")
