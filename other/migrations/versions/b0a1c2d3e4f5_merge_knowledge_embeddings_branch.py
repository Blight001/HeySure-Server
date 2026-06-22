"""merge knowledge embeddings migration branch

Revision ID: b0a1c2d3e4f5
Revises: a8c9d0e1f2b3, a9c8d7e6f5a4
Create Date: 2026-06-21

"""
from typing import Sequence, Union

from alembic import op  # noqa: F401


# revision identifiers, used by Alembic.
revision: str = "b0a1c2d3e4f5"
down_revision: Union[str, Sequence[str], None] = ("a8c9d0e1f2b3", "a9c8d7e6f5a4")
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    pass


def downgrade() -> None:
    pass
