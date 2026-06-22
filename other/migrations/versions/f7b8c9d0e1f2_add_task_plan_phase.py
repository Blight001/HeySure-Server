"""add task plan + phase tables

Revision ID: f7b8c9d0e1f2
Revises: f6a7b8c9d0e1
Create Date: 2026-06-19

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy import inspect
import sqlmodel


# revision identifiers, used by Alembic.
revision: str = "f7b8c9d0e1f2"
down_revision: Union[str, Sequence[str], None] = "f6a7b8c9d0e1"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def _has_table(table_name: str) -> bool:
    return table_name in inspect(op.get_bind()).get_table_names()


def upgrade() -> None:
    if not _has_table("taskplan"):
        op.create_table(
            "taskplan",
            sa.Column("id", sa.Integer(), nullable=False),
            sa.Column("plan_id", sqlmodel.sql.sqltypes.AutoString(), nullable=False),
            sa.Column("user_id", sa.Integer(), nullable=False),
            sa.Column("ai_config_id", sa.Integer(), nullable=False),
            sa.Column("job_id", sqlmodel.sql.sqltypes.AutoString(), nullable=True),
            sa.Column("session_id", sqlmodel.sql.sqltypes.AutoString(), nullable=True),
            sa.Column("goal", sqlmodel.sql.sqltypes.AutoString(), nullable=False),
            sa.Column("status", sqlmodel.sql.sqltypes.AutoString(), nullable=False),
            sa.Column("outcome", sqlmodel.sql.sqltypes.AutoString(), nullable=True),
            sa.Column("phase_count", sa.Integer(), nullable=False),
            sa.Column("current_phase_seq", sa.Integer(), nullable=False),
            sa.Column("summary", sqlmodel.sql.sqltypes.AutoString(), nullable=True),
            sa.Column("created_at", sa.Float(), nullable=False),
            sa.Column("updated_at", sa.Float(), nullable=False),
            sa.Column("finished_at", sa.Float(), nullable=True),
            sa.ForeignKeyConstraint(["user_id"], ["user.id"]),
            sa.PrimaryKeyConstraint("id"),
        )
        with op.batch_alter_table("taskplan", schema=None) as batch_op:
            batch_op.create_index(batch_op.f("ix_taskplan_plan_id"), ["plan_id"], unique=True)
            batch_op.create_index(batch_op.f("ix_taskplan_user_id"), ["user_id"], unique=False)
            batch_op.create_index(batch_op.f("ix_taskplan_ai_config_id"), ["ai_config_id"], unique=False)
            batch_op.create_index(batch_op.f("ix_taskplan_job_id"), ["job_id"], unique=False)
            batch_op.create_index(batch_op.f("ix_taskplan_session_id"), ["session_id"], unique=False)
            batch_op.create_index(batch_op.f("ix_taskplan_status"), ["status"], unique=False)
            batch_op.create_index(batch_op.f("ix_taskplan_created_at"), ["created_at"], unique=False)

    if not _has_table("taskphase"):
        op.create_table(
            "taskphase",
            sa.Column("id", sa.Integer(), nullable=False),
            sa.Column("phase_id", sqlmodel.sql.sqltypes.AutoString(), nullable=False),
            sa.Column("plan_id", sqlmodel.sql.sqltypes.AutoString(), nullable=False),
            sa.Column("user_id", sa.Integer(), nullable=False),
            sa.Column("seq", sa.Integer(), nullable=False),
            sa.Column("title", sqlmodel.sql.sqltypes.AutoString(), nullable=False),
            sa.Column("goal", sqlmodel.sql.sqltypes.AutoString(), nullable=False),
            sa.Column("done_signal", sqlmodel.sql.sqltypes.AutoString(), nullable=False),
            sa.Column("actions_json", sqlmodel.sql.sqltypes.AutoString(), nullable=True),
            sa.Column("status", sqlmodel.sql.sqltypes.AutoString(), nullable=False),
            sa.Column("summary", sqlmodel.sql.sqltypes.AutoString(), nullable=True),
            sa.Column("created_at", sa.Float(), nullable=False),
            sa.Column("started_at", sa.Float(), nullable=True),
            sa.Column("finished_at", sa.Float(), nullable=True),
            sa.PrimaryKeyConstraint("id"),
        )
        with op.batch_alter_table("taskphase", schema=None) as batch_op:
            batch_op.create_index(batch_op.f("ix_taskphase_phase_id"), ["phase_id"], unique=True)
            batch_op.create_index(batch_op.f("ix_taskphase_plan_id"), ["plan_id"], unique=False)
            batch_op.create_index(batch_op.f("ix_taskphase_user_id"), ["user_id"], unique=False)
            batch_op.create_index(batch_op.f("ix_taskphase_seq"), ["seq"], unique=False)
            batch_op.create_index(batch_op.f("ix_taskphase_status"), ["status"], unique=False)


def downgrade() -> None:
    if _has_table("taskphase"):
        with op.batch_alter_table("taskphase", schema=None) as batch_op:
            batch_op.drop_index(batch_op.f("ix_taskphase_status"))
            batch_op.drop_index(batch_op.f("ix_taskphase_seq"))
            batch_op.drop_index(batch_op.f("ix_taskphase_user_id"))
            batch_op.drop_index(batch_op.f("ix_taskphase_plan_id"))
            batch_op.drop_index(batch_op.f("ix_taskphase_phase_id"))
        op.drop_table("taskphase")
    if _has_table("taskplan"):
        with op.batch_alter_table("taskplan", schema=None) as batch_op:
            batch_op.drop_index(batch_op.f("ix_taskplan_created_at"))
            batch_op.drop_index(batch_op.f("ix_taskplan_status"))
            batch_op.drop_index(batch_op.f("ix_taskplan_session_id"))
            batch_op.drop_index(batch_op.f("ix_taskplan_job_id"))
            batch_op.drop_index(batch_op.f("ix_taskplan_ai_config_id"))
            batch_op.drop_index(batch_op.f("ix_taskplan_user_id"))
            batch_op.drop_index(batch_op.f("ix_taskplan_plan_id"))
        op.drop_table("taskplan")
