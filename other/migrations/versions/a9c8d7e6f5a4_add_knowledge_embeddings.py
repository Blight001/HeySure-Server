"""add knowledge embedding index

Revision ID: a9c8d7e6f5a4
Revises: f7b8c9d0e1f2
Create Date: 2026-06-21

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy import inspect
import sqlmodel


# revision identifiers, used by Alembic.
revision: str = "a9c8d7e6f5a4"
down_revision: Union[str, Sequence[str], None] = "f7b8c9d0e1f2"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

_DIMENSIONS = 1536


def _has_table(table_name: str) -> bool:
    return table_name in inspect(op.get_bind()).get_table_names()


def upgrade() -> None:
    if not _has_table("knowledgeembedding"):
        op.create_table(
            "knowledgeembedding",
            sa.Column("id", sa.Integer(), nullable=False),
            sa.Column("memory_id", sqlmodel.sql.sqltypes.AutoString(), nullable=False),
            sa.Column("user_id", sa.Integer(), nullable=False),
            sa.Column("content_hash", sqlmodel.sql.sqltypes.AutoString(), nullable=False),
            sa.Column("content_text", sqlmodel.sql.sqltypes.AutoString(), nullable=False),
            sa.Column("source_snapshot", sqlmodel.sql.sqltypes.AutoString(), nullable=False),
            sa.Column("embedding", sa.JSON(), nullable=False),
            sa.Column("created_at", sa.Float(), nullable=False),
            sa.Column("updated_at", sa.Float(), nullable=False),
            sa.ForeignKeyConstraint(["user_id"], ["user.id"]),
            sa.PrimaryKeyConstraint("id"),
            sa.UniqueConstraint("memory_id"),
        )
        with op.batch_alter_table("knowledgeembedding", schema=None) as batch_op:
            batch_op.create_index(batch_op.f("ix_knowledgeembedding_user_id"), ["user_id"], unique=False)
            batch_op.create_index(batch_op.f("ix_knowledgeembedding_content_hash"), ["content_hash"], unique=False)
            batch_op.create_index(batch_op.f("ix_knowledgeembedding_created_at"), ["created_at"], unique=False)
            batch_op.create_index(batch_op.f("ix_knowledgeembedding_updated_at"), ["updated_at"], unique=False)


def downgrade() -> None:
    if _has_table("knowledgeembedding"):
        with op.batch_alter_table("knowledgeembedding", schema=None) as batch_op:
            batch_op.drop_index(batch_op.f("ix_knowledgeembedding_updated_at"))
            batch_op.drop_index(batch_op.f("ix_knowledgeembedding_created_at"))
            batch_op.drop_index(batch_op.f("ix_knowledgeembedding_content_hash"))
            batch_op.drop_index(batch_op.f("ix_knowledgeembedding_user_id"))
        op.drop_table("knowledgeembedding")
