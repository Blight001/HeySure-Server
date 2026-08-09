"""add explicit worker and connector task leases

Revision ID: e8f9a0b1c2d3
Revises: d7e8f9a0b1c2
Create Date: 2026-08-09
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op


revision: str = "e8f9a0b1c2d3"
down_revision: Union[str, Sequence[str], None] = "d7e8f9a0b1c2"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    with op.batch_alter_table("chatrun") as batch:
        batch.add_column(sa.Column("worker_instance_id", sa.String(), nullable=True))
        batch.add_column(sa.Column("lease_expires_at", sa.Float(), nullable=True))
        batch.add_column(sa.Column("attempt", sa.Integer(), nullable=False, server_default="0"))
        batch.create_index("ix_chatrun_worker_instance_id", ["worker_instance_id"])
        batch.create_index("ix_chatrun_lease_expires_at", ["lease_expires_at"])
    with op.batch_alter_table("agentdispatchtask") as batch:
        batch.add_column(sa.Column("updated_at", sa.Float(), nullable=False, server_default="0"))
        batch.add_column(sa.Column("deadline_at", sa.Float(), nullable=True))
        batch.add_column(sa.Column("owner_instance_id", sa.String(), nullable=True))
        batch.add_column(sa.Column("lease_expires_at", sa.Float(), nullable=True))
        batch.add_column(sa.Column("attempt", sa.Integer(), nullable=False, server_default="0"))
        batch.create_index("ix_agentdispatchtask_deadline_at", ["deadline_at"])
        batch.create_index("ix_agentdispatchtask_owner_instance_id", ["owner_instance_id"])
        batch.create_index("ix_agentdispatchtask_lease_expires_at", ["lease_expires_at"])


def downgrade() -> None:
    with op.batch_alter_table("agentdispatchtask") as batch:
        batch.drop_index("ix_agentdispatchtask_lease_expires_at")
        batch.drop_index("ix_agentdispatchtask_owner_instance_id")
        batch.drop_index("ix_agentdispatchtask_deadline_at")
        batch.drop_column("attempt")
        batch.drop_column("lease_expires_at")
        batch.drop_column("owner_instance_id")
        batch.drop_column("deadline_at")
        batch.drop_column("updated_at")
    with op.batch_alter_table("chatrun") as batch:
        batch.drop_index("ix_chatrun_lease_expires_at")
        batch.drop_index("ix_chatrun_worker_instance_id")
        batch.drop_column("attempt")
        batch.drop_column("lease_expires_at")
        batch.drop_column("worker_instance_id")
