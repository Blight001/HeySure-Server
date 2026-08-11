"""support multi-AI device bindings and per-member MCP scopes

Revision ID: fe5f6a7b8c9d
Revises: fd4e5f6a7b8c
Create Date: 2026-08-11
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op


revision: str = "fe5f6a7b8c9d"
down_revision: Union[str, Sequence[str], None] = "fd4e5f6a7b8c"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def _dedupe(table: str, partition: str) -> None:
    op.execute(
        sa.text(
            f"""
            DELETE FROM {table}
            WHERE id IN (
                SELECT id FROM (
                    SELECT id, ROW_NUMBER() OVER (
                        PARTITION BY {partition}
                        ORDER BY updated_at DESC, id DESC
                    ) AS row_number
                    FROM {table}
                ) ranked
                WHERE ranked.row_number > 1
            )
            """
        )
    )


def upgrade() -> None:
    op.create_table(
        "worlddevicemeta",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("user_id", sa.Integer(), nullable=False),
        sa.Column("device_id", sa.String(), nullable=False),
        sa.Column("sort_order", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("updated_at", sa.Float(), nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_worlddevicemeta_user_id", "worlddevicemeta", ["user_id"])
    op.create_index("ix_worlddevicemeta_device_id", "worlddevicemeta", ["device_id"])
    op.create_index("ix_worlddevicemeta_sort_order", "worlddevicemeta", ["sort_order"])
    op.create_index(
        "uq_worlddevicemeta_user_device",
        "worlddevicemeta",
        ["user_id", "device_id"],
        unique=True,
    )
    op.execute("DELETE FROM deviceaibinding WHERE ai_config_id IS NULL")
    _dedupe("deviceaibinding", "user_id, device_id, ai_config_id")
    _dedupe("workshopaibinding", "user_id, device_id, ai_config_id")
    _dedupe("devicetypemcppermission", "user_id, device_id, ai_config_id")

    op.create_index(
        "uq_deviceaibinding_user_device_ai",
        "deviceaibinding",
        ["user_id", "device_id", "ai_config_id"],
        unique=True,
        postgresql_where=sa.text("ai_config_id IS NOT NULL"),
    )
    op.create_index(
        "uq_workshopaibinding_user_device_ai",
        "workshopaibinding",
        ["user_id", "device_id", "ai_config_id"],
        unique=True,
    )
    op.create_index(
        "uq_devicetypepermission_user_device_ai",
        "devicetypemcppermission",
        ["user_id", "device_id", "ai_config_id"],
        unique=True,
        postgresql_where=sa.text("ai_config_id IS NOT NULL"),
    )
    op.create_index(
        "uq_devicetypepermission_user_device_default",
        "devicetypemcppermission",
        ["user_id", "device_id"],
        unique=True,
        postgresql_where=sa.text("ai_config_id IS NULL"),
    )


def downgrade() -> None:
    for name, table in (
        ("uq_devicetypepermission_user_device_default", "devicetypemcppermission"),
        ("uq_devicetypepermission_user_device_ai", "devicetypemcppermission"),
        ("uq_workshopaibinding_user_device_ai", "workshopaibinding"),
        ("uq_deviceaibinding_user_device_ai", "deviceaibinding"),
    ):
        op.drop_index(name, table_name=table)
    op.drop_table("worlddevicemeta")
