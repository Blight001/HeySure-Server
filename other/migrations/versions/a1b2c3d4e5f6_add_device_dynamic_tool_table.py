"""add device dynamic tool table

Revision ID: a1b2c3d4e5f6
Revises: b6c7d8e9f0a1
Create Date: 2026-06-16

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy import inspect
import sqlmodel


# revision identifiers, used by Alembic.
revision: str = "a1b2c3d4e5f6"
down_revision: Union[str, Sequence[str], None] = "b6c7d8e9f0a1"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def _has_table(table_name: str) -> bool:
    return table_name in inspect(op.get_bind()).get_table_names()


def upgrade() -> None:
    if _has_table("devicedynamictool"):
        return

    op.create_table(
        "devicedynamictool",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("user_id", sa.Integer(), nullable=False),
        sa.Column("device_type", sqlmodel.sql.sqltypes.AutoString(), nullable=False),
        sa.Column("name", sqlmodel.sql.sqltypes.AutoString(), nullable=False),
        sa.Column("description", sqlmodel.sql.sqltypes.AutoString(), nullable=False),
        sa.Column("input_schema_json", sqlmodel.sql.sqltypes.AutoString(), nullable=False),
        sa.Column("code_kind", sqlmodel.sql.sqltypes.AutoString(), nullable=False),
        sa.Column("code_json", sqlmodel.sql.sqltypes.AutoString(), nullable=False),
        sa.Column("js_source", sqlmodel.sql.sqltypes.AutoString(), nullable=False),
        sa.Column("enabled", sa.Boolean(), nullable=False),
        sa.Column("created_at", sa.Float(), nullable=False),
        sa.Column("updated_at", sa.Float(), nullable=False),
        sa.ForeignKeyConstraint(["user_id"], ["user.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    with op.batch_alter_table("devicedynamictool", schema=None) as batch_op:
        batch_op.create_index(batch_op.f("ix_devicedynamictool_user_id"), ["user_id"], unique=False)
        batch_op.create_index(batch_op.f("ix_devicedynamictool_device_type"), ["device_type"], unique=False)
        batch_op.create_index(batch_op.f("ix_devicedynamictool_name"), ["name"], unique=False)


def downgrade() -> None:
    if not _has_table("devicedynamictool"):
        return

    with op.batch_alter_table("devicedynamictool", schema=None) as batch_op:
        batch_op.drop_index(batch_op.f("ix_devicedynamictool_name"))
        batch_op.drop_index(batch_op.f("ix_devicedynamictool_device_type"))
        batch_op.drop_index(batch_op.f("ix_devicedynamictool_user_id"))
    op.drop_table("devicedynamictool")
