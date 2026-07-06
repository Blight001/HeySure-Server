"""add agent mode table and current_mode_key column

Revision ID: c5d6e7f8a9b0
Revises: b3c4d5e6f7a8
Create Date: 2026-07-05

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy import inspect
import sqlmodel


# revision identifiers, used by Alembic.
revision: str = "c5d6e7f8a9b0"
down_revision: Union[str, Sequence[str], None] = "b3c4d5e6f7a8"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def _has_table(table_name: str) -> bool:
    return table_name in inspect(op.get_bind()).get_table_names()


def _has_column(table_name: str, column_name: str) -> bool:
    insp = inspect(op.get_bind())
    if table_name not in insp.get_table_names():
        return False
    return any(col["name"] == column_name for col in insp.get_columns(table_name))


def upgrade() -> None:
    if not _has_table("agentmode"):
        op.create_table(
            "agentmode",
            sa.Column("id", sa.Integer(), nullable=False),
            sa.Column("user_id", sa.Integer(), nullable=False),
            sa.Column("mode_key", sqlmodel.sql.sqltypes.AutoString(), nullable=False),
            sa.Column("name", sqlmodel.sql.sqltypes.AutoString(), nullable=False),
            sa.Column("description", sqlmodel.sql.sqltypes.AutoString(), nullable=False),
            sa.Column("prompt", sqlmodel.sql.sqltypes.AutoString(), nullable=False),
            sa.Column("is_builtin", sa.Boolean(), nullable=False),
            sa.Column("sort_order", sa.Integer(), nullable=False),
            sa.Column("created_at", sa.Float(), nullable=False),
            sa.Column("updated_at", sa.Float(), nullable=False),
            sa.ForeignKeyConstraint(["user_id"], ["user.id"]),
            sa.PrimaryKeyConstraint("id"),
            sa.UniqueConstraint("user_id", "mode_key", name="uq_agentmode_user_key"),
        )
        with op.batch_alter_table("agentmode", schema=None) as batch_op:
            batch_op.create_index(batch_op.f("ix_agentmode_user_id"), ["user_id"], unique=False)
            batch_op.create_index(batch_op.f("ix_agentmode_mode_key"), ["mode_key"], unique=False)

    if not _has_column("assistantaiconfig", "current_mode_key"):
        op.add_column(
            "assistantaiconfig",
            sa.Column(
                "current_mode_key",
                sqlmodel.sql.sqltypes.AutoString(),
                nullable=False,
                server_default="",
            ),
        )
        # Drop the server_default so the column matches the model (app supplies "").
        op.alter_column("assistantaiconfig", "current_mode_key", server_default=None)


def downgrade() -> None:
    if _has_column("assistantaiconfig", "current_mode_key"):
        op.drop_column("assistantaiconfig", "current_mode_key")

    if _has_table("agentmode"):
        with op.batch_alter_table("agentmode", schema=None) as batch_op:
            batch_op.drop_index(batch_op.f("ix_agentmode_mode_key"))
            batch_op.drop_index(batch_op.f("ix_agentmode_user_id"))
        op.drop_table("agentmode")
