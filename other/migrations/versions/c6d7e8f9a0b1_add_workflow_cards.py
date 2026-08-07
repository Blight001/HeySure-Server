"""add workflow cards and persistent runs

Revision ID: c6d7e8f9a0b1
Revises: b4c5d6e7f8a9
Create Date: 2026-08-07
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "c6d7e8f9a0b1"
down_revision: Union[str, Sequence[str], None] = "b4c5d6e7f8a9"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "workflowcard",
        sa.Column("id", sa.String(), nullable=False),
        sa.Column("user_id", sa.Integer(), nullable=False),
        sa.Column("name", sa.String(), nullable=False),
        sa.Column("description", sa.String(), nullable=False),
        sa.Column("status", sa.String(), nullable=False),
        sa.Column("risk_level", sa.String(), nullable=False),
        sa.Column("tags_json", sa.String(), nullable=False),
        sa.Column("draft_definition_json", sa.Text(), nullable=False),
        sa.Column("latest_version_id", sa.String(), nullable=True),
        sa.Column("created_by", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.Float(), nullable=False),
        sa.Column("updated_at", sa.Float(), nullable=False),
        sa.Column("deleted_at", sa.Float(), nullable=True),
        sa.ForeignKeyConstraint(["created_by"], ["user.id"]),
        sa.ForeignKeyConstraint(["user_id"], ["user.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_workflowcard_user_id", "workflowcard", ["user_id"])
    op.create_index("ix_workflowcard_name", "workflowcard", ["name"])
    op.create_index("ix_workflowcard_status", "workflowcard", ["status"])
    op.create_index("ix_workflowcard_latest_version_id", "workflowcard", ["latest_version_id"])
    op.create_index("ix_workflowcard_updated_at", "workflowcard", ["updated_at"])
    op.create_index("ix_workflowcard_deleted_at", "workflowcard", ["deleted_at"])
    op.create_index("ix_workflowcard_user_status", "workflowcard", ["user_id", "status"])

    op.create_table(
        "workflowcardversion",
        sa.Column("id", sa.String(), nullable=False),
        sa.Column("card_id", sa.String(), nullable=False),
        sa.Column("version_number", sa.Integer(), nullable=False),
        sa.Column("schema_version", sa.Integer(), nullable=False),
        sa.Column("definition_json", sa.Text(), nullable=False),
        sa.Column("definition_digest", sa.String(), nullable=False),
        sa.Column("tool_contracts_json", sa.Text(), nullable=False),
        sa.Column("published_by", sa.Integer(), nullable=False),
        sa.Column("published_at", sa.Float(), nullable=False),
        sa.ForeignKeyConstraint(["card_id"], ["workflowcard.id"]),
        sa.ForeignKeyConstraint(["published_by"], ["user.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("card_id", "version_number", name="uq_workflowcardversion_card_number"),
    )
    op.create_index("ix_workflowcardversion_card_id", "workflowcardversion", ["card_id"])
    op.create_index("ix_workflowcardversion_version_number", "workflowcardversion", ["version_number"])
    op.create_index("ix_workflowcardversion_definition_digest", "workflowcardversion", ["definition_digest"])

    op.create_table(
        "workflowrun",
        sa.Column("id", sa.String(), nullable=False),
        sa.Column("card_id", sa.String(), nullable=False),
        sa.Column("card_version_id", sa.String(), nullable=False),
        sa.Column("user_id", sa.Integer(), nullable=False),
        sa.Column("actor_type", sa.String(), nullable=False),
        sa.Column("actor_id", sa.String(), nullable=False),
        sa.Column("device_id", sa.String(), nullable=False),
        sa.Column("status", sa.String(), nullable=False),
        sa.Column("current_step_id", sa.String(), nullable=False),
        sa.Column("transition_count", sa.Integer(), nullable=False),
        sa.Column("input_json", sa.Text(), nullable=False),
        sa.Column("variables_json", sa.Text(), nullable=False),
        sa.Column("output_json", sa.Text(), nullable=True),
        sa.Column("error_json", sa.Text(), nullable=True),
        sa.Column("deadline_at", sa.Float(), nullable=False),
        sa.Column("next_wakeup_at", sa.Float(), nullable=True),
        sa.Column("lock_version", sa.Integer(), nullable=False),
        sa.Column("idempotency_key", sa.String(), nullable=False),
        sa.Column("started_at", sa.Float(), nullable=True),
        sa.Column("finished_at", sa.Float(), nullable=True),
        sa.Column("created_at", sa.Float(), nullable=False),
        sa.Column("updated_at", sa.Float(), nullable=False),
        sa.ForeignKeyConstraint(["card_id"], ["workflowcard.id"]),
        sa.ForeignKeyConstraint(["card_version_id"], ["workflowcardversion.id"]),
        sa.ForeignKeyConstraint(["user_id"], ["user.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("user_id", "idempotency_key", name="uq_workflowrun_user_idempotency"),
    )
    for column in ("card_id", "card_version_id", "user_id", "device_id", "status", "deadline_at", "next_wakeup_at", "idempotency_key", "created_at"):
        op.create_index(f"ix_workflowrun_{column}", "workflowrun", [column])
    op.create_index("ix_workflowrun_status_wakeup", "workflowrun", ["status", "next_wakeup_at"])
    op.create_index("ix_workflowrun_device_status", "workflowrun", ["device_id", "status"])

    op.create_table(
        "workflowsteprun",
        sa.Column("id", sa.String(), nullable=False),
        sa.Column("run_id", sa.String(), nullable=False),
        sa.Column("step_id", sa.String(), nullable=False),
        sa.Column("attempt", sa.Integer(), nullable=False),
        sa.Column("dispatch_task_id", sa.String(), nullable=False),
        sa.Column("tool_name", sa.String(), nullable=False),
        sa.Column("tool_schema_digest", sa.String(), nullable=False),
        sa.Column("status", sa.String(), nullable=False),
        sa.Column("arguments_redacted_json", sa.Text(), nullable=False),
        sa.Column("arguments_json", sa.Text(), nullable=False),
        sa.Column("result_projection_json", sa.Text(), nullable=True),
        sa.Column("result_ref", sa.String(), nullable=True),
        sa.Column("error_json", sa.Text(), nullable=True),
        sa.Column("started_at", sa.Float(), nullable=True),
        sa.Column("deadline_at", sa.Float(), nullable=False),
        sa.Column("finished_at", sa.Float(), nullable=True),
        sa.ForeignKeyConstraint(["run_id"], ["workflowrun.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("dispatch_task_id"),
        sa.UniqueConstraint("run_id", "step_id", "attempt", name="uq_workflowsteprun_attempt"),
    )
    for column in ("run_id", "step_id", "dispatch_task_id", "status", "deadline_at"):
        op.create_index(f"ix_workflowsteprun_{column}", "workflowsteprun", [column])

    op.create_table(
        "workflowconfirmation",
        sa.Column("id", sa.String(), nullable=False),
        sa.Column("run_id", sa.String(), nullable=False),
        sa.Column("step_id", sa.String(), nullable=False),
        sa.Column("status", sa.String(), nullable=False),
        sa.Column("risk_summary", sa.String(), nullable=False),
        sa.Column("requested_user_id", sa.Integer(), nullable=False),
        sa.Column("decided_by", sa.Integer(), nullable=True),
        sa.Column("decision", sa.String(), nullable=True),
        sa.Column("expires_at", sa.Float(), nullable=False),
        sa.Column("created_at", sa.Float(), nullable=False),
        sa.Column("decided_at", sa.Float(), nullable=True),
        sa.ForeignKeyConstraint(["run_id"], ["workflowrun.id"]),
        sa.ForeignKeyConstraint(["requested_user_id"], ["user.id"]),
        sa.ForeignKeyConstraint(["decided_by"], ["user.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    for column in ("run_id", "step_id", "status", "requested_user_id", "expires_at"):
        op.create_index(f"ix_workflowconfirmation_{column}", "workflowconfirmation", [column])


def downgrade() -> None:
    op.drop_table("workflowconfirmation")
    op.drop_table("workflowsteprun")
    op.drop_table("workflowrun")
    op.drop_table("workflowcardversion")
    op.drop_table("workflowcard")
