"""enable inheritance thought tools for legacy empty workshop scopes

Revision ID: c4e81b7a9d22
Revises: 9d8f4a2c6b10
Create Date: 2026-06-13

"""
import json
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy import inspect


revision: str = "c4e81b7a9d22"
down_revision: Union[str, Sequence[str], None] = "9d8f4a2c6b10"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


_TOOLS_JSON = json.dumps(
    [
        "librarian.get_inheritance_thought",
        "librarian.list_inheritance_thoughts",
    ],
    ensure_ascii=False,
)


def upgrade() -> None:
    bind = op.get_bind()
    if "agenttypemcppermission" not in inspect(bind).get_table_names():
        return
    table = sa.table(
        "agenttypemcppermission",
        sa.column("agent_type", sa.String()),
        sa.column("agent_id", sa.String()),
        sa.column("tools_json", sa.String()),
    )
    op.execute(
        table.update()
        .where(
            sa.and_(
                sa.or_(
                    table.c.agent_type == "workshop",
                    table.c.agent_id.like("workshop_builtin_%"),
                ),
                sa.or_(
                    table.c.tools_json.is_(None),
                    table.c.tools_json == "",
                    table.c.tools_json == "[]",
                ),
            )
        )
        .values(tools_json=_TOOLS_JSON)
    )


def downgrade() -> None:
    # Do not erase a permission choice that the user may have changed after upgrade.
    pass
