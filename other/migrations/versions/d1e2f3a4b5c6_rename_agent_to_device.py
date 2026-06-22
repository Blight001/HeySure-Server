"""rename endpoint-agent tables/columns to device

端侧客户端只是"壳"，不具备 agent 能力，故把端侧设备相关的表与列由 agent 改名为
device（与模型 DevicePresence / DeviceAiBinding / DeviceTypeMcpPermission 及字段
device_id / device_type 对齐）：

    endpointagentpresence   -> devicepresence
    agentaibinding          -> deviceaibinding
    agenttypemcppermission  -> devicetypemcppermission
    列 agent_id  -> device_id
    列 agent_type-> device_type

Revision ID: d1e2f3a4b5c6
Revises: e83a4c10f7b6
Create Date: 2026-06-13 14:30:00.000000

"""
from typing import Sequence, Union

from alembic import op


# revision identifiers, used by Alembic.
revision: str = 'd1e2f3a4b5c6'
down_revision: Union[str, Sequence[str], None] = 'e83a4c10f7b6'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


# (old_table, new_table, [(old_col, new_col)...], [(old_index, new_index)...])
_RENAMES = [
    (
        "endpointagentpresence", "devicepresence",
        [("agent_id", "device_id"), ("agent_type", "device_type")],
        [
            ("ix_endpointagentpresence_agent_id", "ix_devicepresence_device_id"),
            ("ix_endpointagentpresence_ai_config_id", "ix_devicepresence_ai_config_id"),
            ("ix_endpointagentpresence_online", "ix_devicepresence_online"),
            ("ix_endpointagentpresence_user_id", "ix_devicepresence_user_id"),
        ],
    ),
    (
        "agentaibinding", "deviceaibinding",
        [("agent_id", "device_id")],
        [
            ("ix_agentaibinding_agent_id", "ix_deviceaibinding_device_id"),
            ("ix_agentaibinding_ai_config_id", "ix_deviceaibinding_ai_config_id"),
            ("ix_agentaibinding_user_id", "ix_deviceaibinding_user_id"),
        ],
    ),
    (
        "agenttypemcppermission", "devicetypemcppermission",
        [("agent_id", "device_id"), ("agent_type", "device_type")],
        [
            ("ix_agenttypemcppermission_agent_id", "ix_devicetypemcppermission_device_id"),
            ("ix_agenttypemcppermission_agent_type", "ix_devicetypemcppermission_device_type"),
            ("ix_agenttypemcppermission_ai_config_id", "ix_devicetypemcppermission_ai_config_id"),
            ("ix_agenttypemcppermission_user_id", "ix_devicetypemcppermission_user_id"),
        ],
    ),
]


def _apply(renames) -> None:
    bind = op.get_bind()
    is_pg = bind.dialect.name == "postgresql"
    for old_table, new_table, cols, indexes in renames:
        if is_pg:
            op.execute(f'ALTER TABLE IF EXISTS "{old_table}" RENAME TO "{new_table}"')
            for old_col, new_col in cols:
                op.execute(f'ALTER TABLE "{new_table}" RENAME COLUMN "{old_col}" TO "{new_col}"')
            for old_idx, new_idx in indexes:
                op.execute(f'ALTER INDEX IF EXISTS "{old_idx}" RENAME TO "{new_idx}"')
        else:
            op.rename_table(old_table, new_table)
            with op.batch_alter_table(new_table) as batch:
                for old_col, new_col in cols:
                    batch.alter_column(old_col, new_column_name=new_col)


def upgrade() -> None:
    """Upgrade schema."""
    _apply(_RENAMES)


def downgrade() -> None:
    """Downgrade schema."""
    reverse = [
        (
            new_table, old_table,
            [(nc, oc) for oc, nc in cols],
            [(ni, oi) for oi, ni in indexes],
        )
        for old_table, new_table, cols, indexes in _RENAMES
    ]
    _apply(reverse)
