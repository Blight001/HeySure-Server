"""add workshop AI binding table

Revision ID: 9d8f4a2c6b10
Revises: 32c720138db6
Create Date: 2026-06-12

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy import inspect
import sqlmodel


# revision identifiers, used by Alembic.
revision: str = "9d8f4a2c6b10"
down_revision: Union[str, Sequence[str], None] = "32c720138db6"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def _has_table(table_name: str) -> bool:
    return table_name in inspect(op.get_bind()).get_table_names()


def upgrade() -> None:
    if _has_table("workshopaibinding"):
        return

    op.create_table(
        "workshopaibinding",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("user_id", sa.Integer(), nullable=False),
        sa.Column("agent_id", sqlmodel.sql.sqltypes.AutoString(), nullable=False),
        sa.Column("ai_config_id", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.Float(), nullable=False),
        sa.Column("updated_at", sa.Float(), nullable=False),
        sa.ForeignKeyConstraint(["ai_config_id"], ["assistantaiconfig.id"]),
        sa.ForeignKeyConstraint(["user_id"], ["user.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    with op.batch_alter_table("workshopaibinding", schema=None) as batch_op:
        batch_op.create_index(batch_op.f("ix_workshopaibinding_agent_id"), ["agent_id"], unique=False)
        batch_op.create_index(batch_op.f("ix_workshopaibinding_ai_config_id"), ["ai_config_id"], unique=False)
        batch_op.create_index(batch_op.f("ix_workshopaibinding_user_id"), ["user_id"], unique=False)


def downgrade() -> None:
    if not _has_table("workshopaibinding"):
        return

    with op.batch_alter_table("workshopaibinding", schema=None) as batch_op:
        batch_op.drop_index(batch_op.f("ix_workshopaibinding_user_id"))
        batch_op.drop_index(batch_op.f("ix_workshopaibinding_ai_config_id"))
        batch_op.drop_index(batch_op.f("ix_workshopaibinding_agent_id"))
    op.drop_table("workshopaibinding")
