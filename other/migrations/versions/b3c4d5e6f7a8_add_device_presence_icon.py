"""add icon to device presence

Devices may pick an icon at register time (a preset under /device_png/ or an
absolute http(s) URL). Stored on the presence row so offline devices keep
their icon in the Workshop panel; empty means the web uses its built-in
per-type rendering.

Revision ID: b3c4d5e6f7a8
Revises: a2b3c4d5e6f7
Create Date: 2026-07-05

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy import inspect
import sqlmodel


# revision identifiers, used by Alembic.
revision: str = "b3c4d5e6f7a8"
down_revision: Union[str, Sequence[str], None] = "a2b3c4d5e6f7"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def _has_table(table_name: str) -> bool:
    return table_name in inspect(op.get_bind()).get_table_names()


def _columns(table_name: str) -> set:
    return {col["name"] for col in inspect(op.get_bind()).get_columns(table_name)}


def upgrade() -> None:
    if not _has_table("devicepresence"):
        return
    if "icon" not in _columns("devicepresence"):
        with op.batch_alter_table("devicepresence", schema=None) as batch_op:
            batch_op.add_column(
                sa.Column("icon", sqlmodel.sql.sqltypes.AutoString(), nullable=False, server_default="")
            )


def downgrade() -> None:
    if not _has_table("devicepresence"):
        return
    if "icon" in _columns("devicepresence"):
        with op.batch_alter_table("devicepresence", schema=None) as batch_op:
            batch_op.drop_column("icon")
