"""add forward_to_bot to chatsession

Revision ID: a8c9d0e1f2b3
Revises: f7b8c9d0e1f2
Create Date: 2026-06-20

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy import inspect


# revision identifiers, used by Alembic.
revision: str = "a8c9d0e1f2b3"
down_revision: Union[str, Sequence[str], None] = "f7b8c9d0e1f2"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def _has_column(table: str, column: str) -> bool:
    insp = inspect(op.get_bind())
    return any(col["name"] == column for col in insp.get_columns(table))


def upgrade() -> None:
    if not _has_column("chatsession", "forward_to_bot"):
        op.add_column(
            "chatsession",
            sa.Column(
                "forward_to_bot",
                sa.Boolean(),
                nullable=False,
                server_default=sa.false(),
            ),
        )
        # Drop the server_default so the ORM default governs future inserts.
        op.alter_column("chatsession", "forward_to_bot", server_default=None)


def downgrade() -> None:
    if _has_column("chatsession", "forward_to_bot"):
        op.drop_column("chatsession", "forward_to_bot")
