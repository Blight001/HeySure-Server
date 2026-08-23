"""add per-user remote controller templates

Revision ID: 7b4c5d6e8f90
Revises: 6a3b4c5d7e8f
Create Date: 2026-08-24
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op


revision: str = "7b4c5d6e8f90"
down_revision: Union[str, Sequence[str], None] = "6a3b4c5d7e8f"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "remotecontrollertemplate",
        sa.Column("id", sa.String(), nullable=False),
        sa.Column("user_id", sa.Integer(), nullable=False),
        sa.Column("template_id", sa.String(), nullable=False),
        sa.Column("document_json", sa.Text(), nullable=False),
        sa.Column("revision", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("builtin_override", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("created_at", sa.Float(), nullable=False),
        sa.Column("updated_at", sa.Float(), nullable=False),
        sa.Column("deleted_at", sa.Float(), nullable=True),
        sa.ForeignKeyConstraint(["user_id"], ["user.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "user_id",
            "template_id",
            name="uq_remote_controller_template_user_template",
        ),
    )
    op.create_index(
        "ix_remotecontrollertemplate_user_id",
        "remotecontrollertemplate",
        ["user_id"],
    )
    op.create_index(
        "ix_remotecontrollertemplate_template_id",
        "remotecontrollertemplate",
        ["template_id"],
    )
    op.create_index(
        "ix_remotecontrollertemplate_builtin_override",
        "remotecontrollertemplate",
        ["builtin_override"],
    )
    op.create_index(
        "ix_remotecontrollertemplate_updated_at",
        "remotecontrollertemplate",
        ["updated_at"],
    )
    op.create_index(
        "ix_remotecontrollertemplate_deleted_at",
        "remotecontrollertemplate",
        ["deleted_at"],
    )


def downgrade() -> None:
    op.drop_index("ix_remotecontrollertemplate_deleted_at", table_name="remotecontrollertemplate")
    op.drop_index("ix_remotecontrollertemplate_updated_at", table_name="remotecontrollertemplate")
    op.drop_index("ix_remotecontrollertemplate_builtin_override", table_name="remotecontrollertemplate")
    op.drop_index("ix_remotecontrollertemplate_template_id", table_name="remotecontrollertemplate")
    op.drop_index("ix_remotecontrollertemplate_user_id", table_name="remotecontrollertemplate")
    op.drop_table("remotecontrollertemplate")
