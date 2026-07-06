"""agentmode per-AI: add ai_config_id and rescope unique key

Revision ID: e7f8a9b0c1d2
Revises: d6e7f8a9b0c1
Create Date: 2026-07-06

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy import inspect


# revision identifiers, used by Alembic.
revision: str = "e7f8a9b0c1d2"
down_revision: Union[str, Sequence[str], None] = "d6e7f8a9b0c1"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def _has_column(table_name: str, column_name: str) -> bool:
    insp = inspect(op.get_bind())
    if table_name not in insp.get_table_names():
        return False
    return any(col["name"] == column_name for col in insp.get_columns(table_name))


def _unique_names(table_name: str) -> set:
    insp = inspect(op.get_bind())
    if table_name not in insp.get_table_names():
        return set()
    return {uc.get("name") for uc in insp.get_unique_constraints(table_name)}


def upgrade() -> None:
    if not _has_column("agentmode", "ai_config_id"):
        op.add_column(
            "agentmode",
            sa.Column("ai_config_id", sa.Integer(), nullable=True),
        )
        with op.batch_alter_table("agentmode", schema=None) as batch_op:
            batch_op.create_index(
                batch_op.f("ix_agentmode_ai_config_id"), ["ai_config_id"], unique=False
            )
    uniques = _unique_names("agentmode")
    if "uq_agentmode_user_key" in uniques:
        op.drop_constraint("uq_agentmode_user_key", "agentmode", type_="unique")
    if "uq_agentmode_user_ai_key" not in uniques:
        op.create_unique_constraint(
            "uq_agentmode_user_ai_key", "agentmode", ["user_id", "ai_config_id", "mode_key"]
        )
    # 存量行保持 ai_config_id=NULL：作为「用户级模板桶」，各 AI 首次播种时从这里
    # 复制（保留用户已有的 prompt 编辑与自定义模式），见 agent_mode_store。


def downgrade() -> None:
    uniques = _unique_names("agentmode")
    if "uq_agentmode_user_ai_key" in uniques:
        op.drop_constraint("uq_agentmode_user_ai_key", "agentmode", type_="unique")
    if _has_column("agentmode", "ai_config_id"):
        # 回退前先清掉按 AI 复制出来的行，避免 (user_id, mode_key) 唯一键冲突。
        op.execute("DELETE FROM agentmode WHERE ai_config_id IS NOT NULL")
        with op.batch_alter_table("agentmode", schema=None) as batch_op:
            batch_op.drop_index(batch_op.f("ix_agentmode_ai_config_id"))
        op.drop_column("agentmode", "ai_config_id")
    if "uq_agentmode_user_key" not in _unique_names("agentmode"):
        op.create_unique_constraint(
            "uq_agentmode_user_key", "agentmode", ["user_id", "mode_key"]
        )
