"""add Huawei push endpoints and durable delivery state

Revision ID: fd4e5f6a7b8c
Revises: fc3d4e5f6a7b
Create Date: 2026-08-11
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op


revision: str = "fd4e5f6a7b8c"
down_revision: Union[str, Sequence[str], None] = "fc3d4e5f6a7b"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "userpushendpoint",
        sa.Column("id", sa.String(), primary_key=True),
        sa.Column("user_id", sa.Integer(), sa.ForeignKey("user.id"), nullable=False),
        sa.Column("provider", sa.String(), nullable=False),
        sa.Column("device_id", sa.String(), nullable=False),
        sa.Column("push_token", sa.String(), nullable=False),
        sa.Column("app_version", sa.String(), nullable=False, server_default=""),
        sa.Column("enabled", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("created_at", sa.Float(), nullable=False),
        sa.Column("updated_at", sa.Float(), nullable=False),
        sa.Column("last_seen_at", sa.Float(), nullable=False),
        sa.UniqueConstraint("provider", "device_id", name="uq_userpushendpoint_provider_device"),
    )
    for name, columns in (
        ("ix_userpushendpoint_user_id", ["user_id"]),
        ("ix_userpushendpoint_provider", ["provider"]),
        ("ix_userpushendpoint_device_id", ["device_id"]),
        ("ix_userpushendpoint_enabled", ["enabled"]),
        ("ix_userpushendpoint_created_at", ["created_at"]),
        ("ix_userpushendpoint_updated_at", ["updated_at"]),
        ("ix_userpushendpoint_last_seen_at", ["last_seen_at"]),
    ):
        op.create_index(name, "userpushendpoint", columns)

    columns = (
        sa.Column("push_status", sa.String(), nullable=False, server_default="not_required"),
        sa.Column("push_attempts", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("push_next_attempt_at", sa.Float(), nullable=False, server_default="0"),
        sa.Column("push_lease_owner", sa.String(), nullable=False, server_default=""),
        sa.Column("push_lease_expires_at", sa.Float(), nullable=False, server_default="0"),
        sa.Column("push_last_error_code", sa.String(), nullable=False, server_default=""),
        sa.Column("push_delivered_at", sa.Float(), nullable=True),
    )
    for column in columns:
        op.add_column("usernotification", column)
    op.execute(
        "UPDATE usernotification SET push_status = 'pending', "
        "push_next_attempt_at = created_at "
        "WHERE app_push_required IS TRUE AND status = 'unread'"
    )
    op.create_index("ix_usernotification_push_status", "usernotification", ["push_status"])
    op.create_index("ix_usernotification_push_next_attempt_at", "usernotification", ["push_next_attempt_at"])
    op.create_index("ix_usernotification_push_lease_expires_at", "usernotification", ["push_lease_expires_at"])
    op.create_index("ix_usernotification_push_delivered_at", "usernotification", ["push_delivered_at"])


def downgrade() -> None:
    for name in (
        "ix_usernotification_push_delivered_at",
        "ix_usernotification_push_lease_expires_at",
        "ix_usernotification_push_next_attempt_at",
        "ix_usernotification_push_status",
    ):
        op.drop_index(name, table_name="usernotification")
    for name in (
        "push_delivered_at",
        "push_last_error_code",
        "push_lease_expires_at",
        "push_lease_owner",
        "push_next_attempt_at",
        "push_attempts",
        "push_status",
    ):
        op.drop_column("usernotification", name)
    op.drop_table("userpushendpoint")
