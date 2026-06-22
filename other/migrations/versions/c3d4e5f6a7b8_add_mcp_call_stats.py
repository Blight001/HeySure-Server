"""add mcp call stats + failure events

Revision ID: c3d4e5f6a7b8
Revises: b2c3d4e5f6a7
Create Date: 2026-06-16

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy import inspect
import sqlmodel


# revision identifiers, used by Alembic.
revision: str = "c3d4e5f6a7b8"
down_revision: Union[str, Sequence[str], None] = "b2c3d4e5f6a7"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def _has_table(table_name: str) -> bool:
    return table_name in inspect(op.get_bind()).get_table_names()


def upgrade() -> None:
    if not _has_table("mcptoolstat"):
        op.create_table(
            "mcptoolstat",
            sa.Column("id", sa.Integer(), nullable=False),
            sa.Column("user_id", sa.Integer(), nullable=False),
            sa.Column("ai_config_id", sa.Integer(), nullable=True),
            sa.Column("tool", sqlmodel.sql.sqltypes.AutoString(), nullable=False),
            sa.Column("total", sa.Integer(), nullable=False),
            sa.Column("failures", sa.Integer(), nullable=False),
            sa.Column("last_called_at", sa.Float(), nullable=False),
            sa.Column("last_failure_at", sa.Float(), nullable=False),
            sa.Column("last_error", sqlmodel.sql.sqltypes.AutoString(), nullable=False),
            sa.ForeignKeyConstraint(["user_id"], ["user.id"]),
            sa.PrimaryKeyConstraint("id"),
        )
        with op.batch_alter_table("mcptoolstat", schema=None) as batch_op:
            batch_op.create_index(batch_op.f("ix_mcptoolstat_user_id"), ["user_id"], unique=False)
            batch_op.create_index(batch_op.f("ix_mcptoolstat_ai_config_id"), ["ai_config_id"], unique=False)
            batch_op.create_index(batch_op.f("ix_mcptoolstat_tool"), ["tool"], unique=False)

    if not _has_table("mcpfailureevent"):
        op.create_table(
            "mcpfailureevent",
            sa.Column("id", sa.Integer(), nullable=False),
            sa.Column("user_id", sa.Integer(), nullable=False),
            sa.Column("ai_config_id", sa.Integer(), nullable=True),
            sa.Column("tool", sqlmodel.sql.sqltypes.AutoString(), nullable=False),
            sa.Column("error", sqlmodel.sql.sqltypes.AutoString(), nullable=False),
            sa.Column("session_id", sqlmodel.sql.sqltypes.AutoString(), nullable=False),
            sa.Column("run_id", sqlmodel.sql.sqltypes.AutoString(), nullable=False),
            sa.Column("message_id", sa.Integer(), nullable=True),
            sa.Column("created_at", sa.Float(), nullable=False),
            sa.ForeignKeyConstraint(["user_id"], ["user.id"]),
            sa.PrimaryKeyConstraint("id"),
        )
        with op.batch_alter_table("mcpfailureevent", schema=None) as batch_op:
            batch_op.create_index(batch_op.f("ix_mcpfailureevent_user_id"), ["user_id"], unique=False)
            batch_op.create_index(batch_op.f("ix_mcpfailureevent_ai_config_id"), ["ai_config_id"], unique=False)
            batch_op.create_index(batch_op.f("ix_mcpfailureevent_tool"), ["tool"], unique=False)
            batch_op.create_index(batch_op.f("ix_mcpfailureevent_created_at"), ["created_at"], unique=False)


def downgrade() -> None:
    if _has_table("mcpfailureevent"):
        with op.batch_alter_table("mcpfailureevent", schema=None) as batch_op:
            batch_op.drop_index(batch_op.f("ix_mcpfailureevent_created_at"))
            batch_op.drop_index(batch_op.f("ix_mcpfailureevent_tool"))
            batch_op.drop_index(batch_op.f("ix_mcpfailureevent_ai_config_id"))
            batch_op.drop_index(batch_op.f("ix_mcpfailureevent_user_id"))
        op.drop_table("mcpfailureevent")
    if _has_table("mcptoolstat"):
        with op.batch_alter_table("mcptoolstat", schema=None) as batch_op:
            batch_op.drop_index(batch_op.f("ix_mcptoolstat_tool"))
            batch_op.drop_index(batch_op.f("ix_mcptoolstat_ai_config_id"))
            batch_op.drop_index(batch_op.f("ix_mcptoolstat_user_id"))
        op.drop_table("mcptoolstat")
