"""add device permission policy table

Per-user, device-type permission policy for runtime tools (allow/confirm/deny
per tag), shipped to devices in the device:tool-config push.

Revision ID: e5f6a7b8c9d0
Revises: d4e5f6a7b8c9
Create Date: 2026-06-17

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy import inspect
import sqlmodel


# revision identifiers, used by Alembic.
revision: str = "e5f6a7b8c9d0"
down_revision: Union[str, Sequence[str], None] = "d4e5f6a7b8c9"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def _has_table(table_name: str) -> bool:
    return table_name in inspect(op.get_bind()).get_table_names()


def upgrade() -> None:
    if _has_table("devicepermissionpolicy"):
        return

    op.create_table(
        "devicepermissionpolicy",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("user_id", sa.Integer(), nullable=False),
        sa.Column("device_type", sqlmodel.sql.sqltypes.AutoString(), nullable=False),
        sa.Column("policy_json", sqlmodel.sql.sqltypes.AutoString(), nullable=False),
        sa.Column("updated_at", sa.Float(), nullable=False),
        sa.ForeignKeyConstraint(["user_id"], ["user.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    with op.batch_alter_table("devicepermissionpolicy", schema=None) as batch_op:
        batch_op.create_index(batch_op.f("ix_devicepermissionpolicy_user_id"), ["user_id"], unique=False)
        batch_op.create_index(batch_op.f("ix_devicepermissionpolicy_device_type"), ["device_type"], unique=False)


def downgrade() -> None:
    if not _has_table("devicepermissionpolicy"):
        return

    with op.batch_alter_table("devicepermissionpolicy", schema=None) as batch_op:
        batch_op.drop_index(batch_op.f("ix_devicepermissionpolicy_device_type"))
        batch_op.drop_index(batch_op.f("ix_devicepermissionpolicy_user_id"))
    op.drop_table("devicepermissionpolicy")
