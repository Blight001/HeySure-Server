"""add per-conversation model preset override

Revision ID: b4c5d6e7f8a9
Revises: a3b4c5d6e7f8
Create Date: 2026-07-19

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy import inspect
import sqlmodel


revision: str = "b4c5d6e7f8a9"
down_revision: Union[str, Sequence[str], None] = "a3b4c5d6e7f8"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def _has_column(table: str, column: str) -> bool:
    insp = inspect(op.get_bind())
    return table in insp.get_table_names() and any(
        item["name"] == column for item in insp.get_columns(table)
    )


def upgrade() -> None:
    if not _has_column("chatsession", "model_preset_id"):
        op.add_column(
            "chatsession",
            sa.Column(
                "model_preset_id",
                sqlmodel.sql.sqltypes.AutoString(),
                nullable=False,
                server_default="",
            ),
        )
        op.create_index(
            op.f("ix_chatsession_model_preset_id"),
            "chatsession",
            ["model_preset_id"],
            unique=False,
        )
        op.alter_column("chatsession", "model_preset_id", server_default=None)


def downgrade() -> None:
    if _has_column("chatsession", "model_preset_id"):
        op.drop_index(op.f("ix_chatsession_model_preset_id"), table_name="chatsession")
        op.drop_column("chatsession", "model_preset_id")
