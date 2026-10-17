"""merge remote-controller and Codex bridge migration branches

Revision ID: g0h1i2j3k4l5
Revises: 7b4c5d6e8f90, fg7b8c9d0e1f
"""

from typing import Sequence, Union


revision: str = "g0h1i2j3k4l5"
down_revision: Union[str, Sequence[str], None] = ("7b4c5d6e8f90", "fg7b8c9d0e1f")
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Merge the two already-complete branches without changing schema."""


def downgrade() -> None:
    """Re-expose the two parent heads when downgrading past the merge."""
