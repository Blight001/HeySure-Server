"""add device dynamic tool version table

Revision ID: b2c3d4e5f6a7
Revises: a1b2c3d4e5f6
Create Date: 2026-06-16

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy import inspect
import sqlmodel


# revision identifiers, used by Alembic.
revision: str = "b2c3d4e5f6a7"
down_revision: Union[str, Sequence[str], None] = "a1b2c3d4e5f6"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def _has_table(table_name: str) -> bool:
    return table_name in inspect(op.get_bind()).get_table_names()


def upgrade() -> None:
    if _has_table("devicedynamictoolversion"):
        return

    op.create_table(
        "devicedynamictoolversion",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("user_id", sa.Integer(), nullable=False),
        sa.Column("device_type", sqlmodel.sql.sqltypes.AutoString(), nullable=False),
        sa.Column("name", sqlmodel.sql.sqltypes.AutoString(), nullable=False),
        sa.Column("revision", sqlmodel.sql.sqltypes.AutoString(), nullable=False),
        sa.Column("action", sqlmodel.sql.sqltypes.AutoString(), nullable=False),
        sa.Column("actor", sqlmodel.sql.sqltypes.AutoString(), nullable=False),
        sa.Column("ai_config_id", sa.Integer(), nullable=True),
        sa.Column("description", sqlmodel.sql.sqltypes.AutoString(), nullable=False),
        sa.Column("input_schema_json", sqlmodel.sql.sqltypes.AutoString(), nullable=False),
        sa.Column("code_kind", sqlmodel.sql.sqltypes.AutoString(), nullable=False),
        sa.Column("code_json", sqlmodel.sql.sqltypes.AutoString(), nullable=False),
        sa.Column("js_source", sqlmodel.sql.sqltypes.AutoString(), nullable=False),
        sa.Column("created_at", sa.Float(), nullable=False),
        sa.ForeignKeyConstraint(["user_id"], ["user.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    with op.batch_alter_table("devicedynamictoolversion", schema=None) as batch_op:
        batch_op.create_index(batch_op.f("ix_devicedynamictoolversion_user_id"), ["user_id"], unique=False)
        batch_op.create_index(batch_op.f("ix_devicedynamictoolversion_device_type"), ["device_type"], unique=False)
        batch_op.create_index(batch_op.f("ix_devicedynamictoolversion_name"), ["name"], unique=False)
        batch_op.create_index(batch_op.f("ix_devicedynamictoolversion_ai_config_id"), ["ai_config_id"], unique=False)
        batch_op.create_index(batch_op.f("ix_devicedynamictoolversion_created_at"), ["created_at"], unique=False)


def downgrade() -> None:
    if not _has_table("devicedynamictoolversion"):
        return

    with op.batch_alter_table("devicedynamictoolversion", schema=None) as batch_op:
        batch_op.drop_index(batch_op.f("ix_devicedynamictoolversion_created_at"))
        batch_op.drop_index(batch_op.f("ix_devicedynamictoolversion_ai_config_id"))
        batch_op.drop_index(batch_op.f("ix_devicedynamictoolversion_name"))
        batch_op.drop_index(batch_op.f("ix_devicedynamictoolversion_device_type"))
        batch_op.drop_index(batch_op.f("ix_devicedynamictoolversion_user_id"))
    op.drop_table("devicedynamictoolversion")
