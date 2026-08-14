"""add atomic device capability catalog metadata

Revision ID: f52a8c3d1e40
Revises: e41f7b2c9a10
Create Date: 2026-08-14
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op


revision: str = "f52a8c3d1e40"
down_revision: Union[str, Sequence[str], None] = "e41f7b2c9a10"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "devicepresence",
        sa.Column("reported_ai_description", sa.Text(), nullable=False, server_default=""),
    )
    op.add_column(
        "devicepresence",
        sa.Column("ai_description_override", sa.Text(), nullable=False, server_default=""),
    )
    op.add_column(
        "devicepresence",
        sa.Column("catalog_generation", sa.BigInteger(), nullable=False, server_default="0"),
    )
    op.add_column(
        "devicepresence",
        sa.Column("catalog_hash", sa.String(length=64), nullable=False, server_default=""),
    )
    op.add_column(
        "devicepresence",
        sa.Column("catalog_protocol_version", sa.Integer(), nullable=False, server_default="1"),
    )

    # Presence is a replaceable snapshot. Historical code already retained the
    # latest duplicate row; make that invariant enforceable under concurrent
    # reconnects before turning the existing lookup index into a unique one.
    op.execute(sa.text("""
        DELETE FROM devicepresence AS stale
        USING devicepresence AS current
        WHERE stale.device_id = current.device_id
          AND (stale.updated_at, stale.id) < (current.updated_at, current.id)
    """))
    op.drop_index("ix_devicepresence_device_id", table_name="devicepresence")
    op.create_index(
        "ix_devicepresence_device_id", "devicepresence", ["device_id"], unique=True
    )


def downgrade() -> None:
    op.drop_index("ix_devicepresence_device_id", table_name="devicepresence")
    op.create_index(
        "ix_devicepresence_device_id", "devicepresence", ["device_id"], unique=False
    )
    op.drop_column("devicepresence", "catalog_protocol_version")
    op.drop_column("devicepresence", "catalog_hash")
    op.drop_column("devicepresence", "catalog_generation")
    op.drop_column("devicepresence", "ai_description_override")
    op.drop_column("devicepresence", "reported_ai_description")
