"""rename workshopaibinding/agentdispatchtask agent_id column to device_id

补迁移：上一条 revision (d1e2f3a4b5c6) 只改了 endpointagentpresence/agentaibinding/
agenttypemcppermission 三张表，漏了同样把模型字段 agent_id 改成 device_id 的
``workshopaibinding`` 与 ``agentdispatchtask`` 两张表，导致 WorkshopAiBinding 查询
报"column device_id 不存在"、知识工坊不显示。本 revision 按列存在性幂等修复，无论
上一条是否已应用都安全。

Revision ID: e4a5b6c7d8e9
Revises: d1e2f3a4b5c6
Create Date: 2026-06-13 15:10:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'e4a5b6c7d8e9'
down_revision: Union[str, Sequence[str], None] = 'd1e2f3a4b5c6'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


# (table, old_col, new_col, old_index_or_None, new_index_or_None)
_COLS = [
    ("workshopaibinding", "agent_id", "device_id",
     "ix_workshopaibinding_agent_id", "ix_workshopaibinding_device_id"),
    ("agentdispatchtask", "agent_id", "device_id", None, None),
]


def _columns(insp, table) -> set:
    try:
        return {c["name"] for c in insp.get_columns(table)}
    except Exception:
        return set()


def _rename(pairs) -> None:
    bind = op.get_bind()
    insp = sa.inspect(bind)
    is_pg = bind.dialect.name == "postgresql"
    tables = set(insp.get_table_names())
    for table, old_c, new_c, old_i, new_i in pairs:
        if table not in tables:
            continue
        cols = _columns(insp, table)
        if old_c not in cols or new_c in cols:
            continue  # already renamed or column missing — idempotent no-op
        if is_pg:
            op.execute(f'ALTER TABLE "{table}" RENAME COLUMN "{old_c}" TO "{new_c}"')
            if old_i:
                op.execute(f'ALTER INDEX IF EXISTS "{old_i}" RENAME TO "{new_i}"')
        else:
            with op.batch_alter_table(table) as batch:
                batch.alter_column(old_c, new_column_name=new_c)


def upgrade() -> None:
    """Upgrade schema."""
    _rename(_COLS)


def downgrade() -> None:
    """Downgrade schema."""
    _rename([(t, nc, oc, ni, oi) for (t, oc, nc, oi, ni) in _COLS])
