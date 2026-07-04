"""add name/platform to device presence

Lets a device that has gone offline still show up (with its last-known name)
in the web Workshop ("作坊") panel, so an operator can save/assign a record for
it while it's disconnected — it takes effect on the device's next reconnect.
Existing rows default to an empty string; they backfill on the device's next
``device:register``.

Revision ID: a2b3c4d5e6f7
Revises: f1a2b3c4d5e6
Create Date: 2026-07-04

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy import inspect
import sqlmodel


# revision identifiers, used by Alembic.
revision: str = "a2b3c4d5e6f7"
down_revision: Union[str, Sequence[str], None] = "f1a2b3c4d5e6"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def _has_table(table_name: str) -> bool:
    return table_name in inspect(op.get_bind()).get_table_names()


def _columns(table_name: str) -> set:
    return {col["name"] for col in inspect(op.get_bind()).get_columns(table_name)}


def upgrade() -> None:
    if not _has_table("devicepresence"):
        return
    existing = _columns("devicepresence")
    with op.batch_alter_table("devicepresence", schema=None) as batch_op:
        if "name" not in existing:
            batch_op.add_column(
                sa.Column("name", sqlmodel.sql.sqltypes.AutoString(), nullable=False, server_default="")
            )
        if "platform" not in existing:
            batch_op.add_column(
                sa.Column("platform", sqlmodel.sql.sqltypes.AutoString(), nullable=False, server_default="")
            )


def downgrade() -> None:
    if not _has_table("devicepresence"):
        return
    existing = _columns("devicepresence")
    with op.batch_alter_table("devicepresence", schema=None) as batch_op:
        if "platform" in existing:
            batch_op.drop_column("platform")
        if "name" in existing:
            batch_op.drop_column("name")
