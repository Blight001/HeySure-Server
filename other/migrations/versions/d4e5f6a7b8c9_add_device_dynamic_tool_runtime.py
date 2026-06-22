"""add runtime/source/permissions to device dynamic tool (+ version)

Lets a web- or AI-authored device tool ship plain runtime code (python /
powershell / shell) plus declared permission tags, executed by the device's
controlled runner base. Mirrors the device interpreter's ``code_kind="runtime"``.

Revision ID: d4e5f6a7b8c9
Revises: c3d4e5f6a7b8
Create Date: 2026-06-17

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy import inspect
import sqlmodel


# revision identifiers, used by Alembic.
revision: str = "d4e5f6a7b8c9"
down_revision: Union[str, Sequence[str], None] = "c3d4e5f6a7b8"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


_TABLES = ("devicedynamictool", "devicedynamictoolversion")
_NEW_COLUMNS = (
    ("runtime", "''"),
    ("source", "''"),
    ("permissions_json", "'[]'"),
)


def _has_table(table_name: str) -> bool:
    return table_name in inspect(op.get_bind()).get_table_names()


def _columns(table_name: str) -> set:
    return {col["name"] for col in inspect(op.get_bind()).get_columns(table_name)}


def upgrade() -> None:
    for table in _TABLES:
        if not _has_table(table):
            continue
        existing = _columns(table)
        with op.batch_alter_table(table, schema=None) as batch_op:
            for name, default in _NEW_COLUMNS:
                if name in existing:
                    continue
                batch_op.add_column(
                    sa.Column(
                        name,
                        sqlmodel.sql.sqltypes.AutoString(),
                        nullable=False,
                        server_default=default,
                    )
                )


def downgrade() -> None:
    for table in _TABLES:
        if not _has_table(table):
            continue
        existing = _columns(table)
        with op.batch_alter_table(table, schema=None) as batch_op:
            for name, _default in _NEW_COLUMNS:
                if name in existing:
                    batch_op.drop_column(name)
