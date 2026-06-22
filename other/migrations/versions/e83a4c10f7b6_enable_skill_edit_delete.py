"""enable inheritance thought editing and deletion for default scopes

Revision ID: e83a4c10f7b6
Revises: d72f93a61e04
Create Date: 2026-06-13

"""
import json
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy import inspect


revision: str = "e83a4c10f7b6"
down_revision: Union[str, Sequence[str], None] = "d72f93a61e04"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


_OLD_TOOLS = [
    "librarian.get_inheritance_thought",
    "librarian.install_skill_package",
    "librarian.list_inheritance_thoughts",
]
_NEW_TOOLS = [
    "librarian.delete_inheritance_thought",
    "librarian.edit_inheritance_thought",
    "librarian.get_inheritance_thought",
    "librarian.install_skill_package",
    "librarian.list_inheritance_thoughts",
]


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
                table.c.tools_json == json.dumps(_OLD_TOOLS, ensure_ascii=False),
            )
        )
        .values(tools_json=json.dumps(_NEW_TOOLS, ensure_ascii=False))
    )


def downgrade() -> None:
    pass
