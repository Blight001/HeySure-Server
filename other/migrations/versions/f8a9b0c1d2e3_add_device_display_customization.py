"""add device display customization

Operators can set a Workshop-panel remark and icon override for endpoint
devices. The override is intentionally separate from the icon reported by the
device at register time, so reconnects do not erase the user's choice.

Revision ID: f8a9b0c1d2e3
Revises: e7f8a9b0c1d2
Create Date: 2026-07-09

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy import inspect
import sqlmodel


# revision identifiers, used by Alembic.
revision: str = "f8a9b0c1d2e3"
down_revision: Union[str, Sequence[str], None] = "e7f8a9b0c1d2"
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
        if "remark" not in existing:
            batch_op.add_column(
                sa.Column("remark", sqlmodel.sql.sqltypes.AutoString(), nullable=False, server_default="")
            )
        if "icon_override" not in existing:
            batch_op.add_column(
                sa.Column("icon_override", sqlmodel.sql.sqltypes.AutoString(), nullable=False, server_default="")
            )


def downgrade() -> None:
    if not _has_table("devicepresence"):
        return
    existing = _columns("devicepresence")
    with op.batch_alter_table("devicepresence", schema=None) as batch_op:
        if "icon_override" in existing:
            batch_op.drop_column("icon_override")
        if "remark" in existing:
            batch_op.drop_column("remark")
