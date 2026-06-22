"""add status (approval lifecycle) to device dynamic tool

AI-authored tools land as 'draft' and only ship once an operator approves them
to 'active' (设备端MCP代码下放长期方案 §7.3 / 阶段五). Existing rows default to
'active' so behaviour is unchanged after upgrade.

Revision ID: f6a7b8c9d0e1
Revises: e5f6a7b8c9d0
Create Date: 2026-06-17

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy import inspect
import sqlmodel


# revision identifiers, used by Alembic.
revision: str = "f6a7b8c9d0e1"
down_revision: Union[str, Sequence[str], None] = "e5f6a7b8c9d0"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def _has_table(table_name: str) -> bool:
    return table_name in inspect(op.get_bind()).get_table_names()


def _columns(table_name: str) -> set:
    return {col["name"] for col in inspect(op.get_bind()).get_columns(table_name)}


def upgrade() -> None:
    if not _has_table("devicedynamictool"):
        return
    if "status" in _columns("devicedynamictool"):
        return
    with op.batch_alter_table("devicedynamictool", schema=None) as batch_op:
        batch_op.add_column(
            sa.Column(
                "status",
                sqlmodel.sql.sqltypes.AutoString(),
                nullable=False,
                server_default="active",
            )
        )
        batch_op.create_index(batch_op.f("ix_devicedynamictool_status"), ["status"], unique=False)


def downgrade() -> None:
    if not _has_table("devicedynamictool"):
        return
    if "status" not in _columns("devicedynamictool"):
        return
    with op.batch_alter_table("devicedynamictool", schema=None) as batch_op:
        batch_op.drop_index(batch_op.f("ix_devicedynamictool_status"))
        batch_op.drop_column("status")
