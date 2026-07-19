"""drop agentmode table and assistantaiconfig.current_mode_key column

「工作模式」系统已整体移除：不再有运行时工具门禁 / mode.manage 工具 / 模式 prompt 注入 /
前端模式 UI。此迁移删除对应的数据库表与列。

Revision ID: a3b4c5d6e7f8
Revises: 2b3c4d5e6f70
Create Date: 2026-07-19

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy import inspect
import sqlmodel


# revision identifiers, used by Alembic.
revision: str = "a3b4c5d6e7f8"
down_revision: Union[str, Sequence[str], None] = "2b3c4d5e6f70"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def _has_table(table_name: str) -> bool:
    return table_name in inspect(op.get_bind()).get_table_names()


def _has_column(table_name: str, column_name: str) -> bool:
    insp = inspect(op.get_bind())
    if table_name not in insp.get_table_names():
        return False
    return any(col["name"] == column_name for col in insp.get_columns(table_name))


def _index_names(table_name: str) -> set:
    insp = inspect(op.get_bind())
    if table_name not in insp.get_table_names():
        return set()
    return {ix.get("name") for ix in insp.get_indexes(table_name)}


def upgrade() -> None:
    if _has_column("assistantaiconfig", "current_mode_key"):
        op.drop_column("assistantaiconfig", "current_mode_key")

    if _has_table("agentmode"):
        existing = _index_names("agentmode")
        with op.batch_alter_table("agentmode", schema=None) as batch_op:
            for ix in (
                "ix_agentmode_ai_config_id",
                "ix_agentmode_mode_key",
                "ix_agentmode_user_id",
            ):
                if ix in existing:
                    batch_op.drop_index(batch_op.f(ix))
        op.drop_table("agentmode")


def downgrade() -> None:
    # 回退：重建最终状态的 agentmode 表（含 ai_config_id / allow_device_mcp）与
    # assistantaiconfig.current_mode_key 列。
    if not _has_table("agentmode"):
        op.create_table(
            "agentmode",
            sa.Column("id", sa.Integer(), nullable=False),
            sa.Column("user_id", sa.Integer(), nullable=False),
            sa.Column("ai_config_id", sa.Integer(), nullable=True),
            sa.Column("mode_key", sqlmodel.sql.sqltypes.AutoString(), nullable=False),
            sa.Column("name", sqlmodel.sql.sqltypes.AutoString(), nullable=False),
            sa.Column("description", sqlmodel.sql.sqltypes.AutoString(), nullable=False),
            sa.Column("prompt", sqlmodel.sql.sqltypes.AutoString(), nullable=False),
            sa.Column("allow_device_mcp", sa.Boolean(), nullable=False, server_default=sa.true()),
            sa.Column("is_builtin", sa.Boolean(), nullable=False),
            sa.Column("sort_order", sa.Integer(), nullable=False),
            sa.Column("created_at", sa.Float(), nullable=False),
            sa.Column("updated_at", sa.Float(), nullable=False),
            sa.ForeignKeyConstraint(["user_id"], ["user.id"]),
            sa.PrimaryKeyConstraint("id"),
            sa.UniqueConstraint(
                "user_id", "ai_config_id", "mode_key", name="uq_agentmode_user_ai_key"
            ),
        )
        with op.batch_alter_table("agentmode", schema=None) as batch_op:
            batch_op.create_index(batch_op.f("ix_agentmode_user_id"), ["user_id"], unique=False)
            batch_op.create_index(batch_op.f("ix_agentmode_mode_key"), ["mode_key"], unique=False)
            batch_op.create_index(
                batch_op.f("ix_agentmode_ai_config_id"), ["ai_config_id"], unique=False
            )
        op.alter_column("agentmode", "allow_device_mcp", server_default=None)

    if not _has_column("assistantaiconfig", "current_mode_key"):
        op.add_column(
            "assistantaiconfig",
            sa.Column(
                "current_mode_key",
                sqlmodel.sql.sqltypes.AutoString(),
                nullable=False,
                server_default="initial",
            ),
        )
        op.alter_column("assistantaiconfig", "current_mode_key", server_default=None)
