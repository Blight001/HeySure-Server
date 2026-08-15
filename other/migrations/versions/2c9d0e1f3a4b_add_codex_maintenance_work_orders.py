"""add durable Codex maintenance work orders

Revision ID: 2c9d0e1f3a4b
Revises: 1b8c9d0e2f3a
Create Date: 2026-08-15
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op


revision: str = "2c9d0e1f3a4b"
down_revision: Union[str, Sequence[str], None] = "1b8c9d0e2f3a"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def _indexes(table: str, definitions: tuple[tuple[str, list[str]], ...]) -> None:
    for name, columns in definitions:
        op.create_index(name, table, columns)


def upgrade() -> None:
    op.create_table(
        "maintenancetask",
        sa.Column("task_id", sa.String(), primary_key=True),
        sa.Column("run_id", sa.String(), nullable=False, unique=True),
        sa.Column("user_id", sa.Integer(), sa.ForeignKey("user.id"), nullable=False),
        sa.Column("maintainer_ai_config_id", sa.Integer(), sa.ForeignKey("assistantaiconfig.id"), nullable=False),
        sa.Column("reporter_ai_config_id", sa.Integer(), sa.ForeignKey("assistantaiconfig.id"), nullable=True),
        sa.Column("source_session_id", sa.String(), nullable=False, server_default=""),
        sa.Column("device_id", sa.String(), nullable=False),
        sa.Column("title", sa.String(), nullable=False, server_default=""),
        sa.Column("description", sa.Text(), nullable=False, server_default=""),
        sa.Column("acceptance_criteria", sa.Text(), nullable=False, server_default=""),
        sa.Column("affected_repo", sa.String(), nullable=False, server_default=""),
        sa.Column("branch_name", sa.String(), nullable=False, server_default=""),
        sa.Column("base_sha", sa.String(), nullable=False, server_default=""),
        sa.Column("severity", sa.String(), nullable=False, server_default="normal"),
        sa.Column("dedupe_key", sa.String(), nullable=False, server_default=""),
        sa.Column("status", sa.String(), nullable=False, server_default="queued"),
        sa.Column("phase", sa.String(), nullable=False, server_default="triage"),
        sa.Column("owner", sa.String(), nullable=False, server_default=""),
        sa.Column("lease_expires_at", sa.Float(), nullable=True),
        sa.Column("deadline_at", sa.Float(), nullable=True),
        sa.Column("last_sequence", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("last_device_sequence", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("summary", sa.Text(), nullable=False, server_default=""),
        sa.Column("error_code", sa.String(), nullable=False, server_default=""),
        sa.Column("created_at", sa.Float(), nullable=False),
        sa.Column("updated_at", sa.Float(), nullable=False),
        sa.Column("started_at", sa.Float(), nullable=True),
        sa.Column("finished_at", sa.Float(), nullable=True),
    )
    _indexes("maintenancetask", tuple(
        (f"ix_maintenancetask_{name}", [name]) for name in (
            "run_id", "user_id", "maintainer_ai_config_id", "reporter_ai_config_id",
            "source_session_id", "device_id", "affected_repo", "branch_name", "base_sha",
            "severity", "dedupe_key",
            "status", "phase", "lease_expires_at", "deadline_at", "created_at", "updated_at",
        )
    ))
    op.create_table(
        "maintenanceevent",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("task_id", sa.String(), sa.ForeignKey("maintenancetask.task_id", ondelete="CASCADE"), nullable=False),
        sa.Column("run_id", sa.String(), nullable=False),
        sa.Column("event_id", sa.String(), nullable=False),
        sa.Column("sequence", sa.Integer(), nullable=False),
        sa.Column("event_type", sa.String(), nullable=False),
        sa.Column("actor_type", sa.String(), nullable=False, server_default="system"),
        sa.Column("actor_id", sa.String(), nullable=False, server_default=""),
        sa.Column("phase", sa.String(), nullable=False, server_default=""),
        sa.Column("status", sa.String(), nullable=False, server_default=""),
        sa.Column("payload_json", sa.Text(), nullable=False, server_default="{}"),
        sa.Column("created_at", sa.Float(), nullable=False),
        sa.UniqueConstraint("run_id", "event_id", name="uq_maintenanceevent_run_event"),
        sa.UniqueConstraint("run_id", "sequence", name="uq_maintenanceevent_run_sequence"),
    )
    _indexes("maintenanceevent", tuple(
        (f"ix_maintenanceevent_{name}", [name]) for name in (
            "task_id", "run_id", "event_id", "sequence", "event_type", "actor_type",
            "phase", "status", "created_at",
        )
    ))
    op.create_table(
        "maintenanceapproval",
        sa.Column("approval_id", sa.String(), primary_key=True),
        sa.Column("task_id", sa.String(), sa.ForeignKey("maintenancetask.task_id", ondelete="CASCADE"), nullable=False),
        sa.Column("run_id", sa.String(), nullable=False),
        sa.Column("user_id", sa.Integer(), sa.ForeignKey("user.id"), nullable=False),
        sa.Column("request_event_id", sa.String(), nullable=False),
        sa.Column("approval_type", sa.String(), nullable=False),
        sa.Column("title", sa.String(), nullable=False, server_default=""),
        sa.Column("detail_json", sa.Text(), nullable=False, server_default="{}"),
        sa.Column("status", sa.String(), nullable=False, server_default="pending"),
        sa.Column("decision", sa.String(), nullable=False, server_default=""),
        sa.Column("decided_by_user_id", sa.Integer(), sa.ForeignKey("user.id"), nullable=True),
        sa.Column("created_at", sa.Float(), nullable=False),
        sa.Column("expires_at", sa.Float(), nullable=True),
        sa.Column("decided_at", sa.Float(), nullable=True),
    )
    _indexes("maintenanceapproval", tuple(
        (f"ix_maintenanceapproval_{name}", [name]) for name in (
            "task_id", "run_id", "user_id", "request_event_id", "approval_type",
            "status", "created_at", "expires_at",
        )
    ))


def downgrade() -> None:
    op.drop_table("maintenanceapproval")
    op.drop_table("maintenanceevent")
    op.drop_table("maintenancetask")
