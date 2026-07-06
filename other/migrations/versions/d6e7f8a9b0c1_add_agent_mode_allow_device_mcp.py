"""add agentmode.allow_device_mcp mode-type column

Revision ID: d6e7f8a9b0c1
Revises: c5d6e7f8a9b0
Create Date: 2026-07-06

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy import inspect


# revision identifiers, used by Alembic.
revision: str = "d6e7f8a9b0c1"
down_revision: Union[str, Sequence[str], None] = "c5d6e7f8a9b0"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def _has_column(table_name: str, column_name: str) -> bool:
    insp = inspect(op.get_bind())
    if table_name not in insp.get_table_names():
        return False
    return any(col["name"] == column_name for col in insp.get_columns(table_name))


def upgrade() -> None:
    if not _has_column("agentmode", "allow_device_mcp"):
        op.add_column(
            "agentmode",
            sa.Column(
                "allow_device_mcp",
                sa.Boolean(),
                nullable=False,
                server_default=sa.true(),
            ),
        )
        # 存量行按旧的硬编码规则回填：initial / 旧 chat 属对话模式，不允许设备端 MCP。
        op.execute(
            "UPDATE agentmode SET allow_device_mcp = FALSE "
            "WHERE mode_key IN ('initial', 'chat')"
        )
        # Drop the server_default so the column matches the model (app supplies True).
        op.alter_column("agentmode", "allow_device_mcp", server_default=None)


def downgrade() -> None:
    if _has_column("agentmode", "allow_device_mcp"):
        op.drop_column("agentmode", "allow_device_mcp")
