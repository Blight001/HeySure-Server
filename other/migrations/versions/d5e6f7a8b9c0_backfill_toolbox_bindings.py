"""backfill toolbox bindings for all existing AIs

The 工具箱 (toolbox) becomes a bindable built-in workshop: an AI must be bound to
its toolbox device (``toolbox_builtin_<user_id>``) to use the default server MCP
tools. New AIs are auto-bound on creation; this one-time migration backfills the
binding for every AI that already existed, so upgraded deployments don't suddenly
gate their AIs out of the toolbox tool set.

Idempotent within itself (skips AIs already bound), and runs once via Alembic
revision tracking — so a user's later manual *unbind* is not re-bound on restart.

Revision ID: d5e6f7a8b9c0
Revises: c1d2e3f4a5b6
Create Date: 2026-06-21

"""
import time
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy import inspect


# revision identifiers, used by Alembic.
revision: str = "d5e6f7a8b9c0"
down_revision: Union[str, Sequence[str], None] = "c1d2e3f4a5b6"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    bind = op.get_bind()
    tables = set(inspect(bind).get_table_names())
    if "assistantaiconfig" not in tables or "workshopaibinding" not in tables:
        return

    configs = bind.execute(sa.text("SELECT id, user_id FROM assistantaiconfig")).fetchall()
    existing = bind.execute(
        sa.text(
            "SELECT user_id, ai_config_id FROM workshopaibinding "
            "WHERE device_id LIKE 'toolbox_builtin_%'"
        )
    ).fetchall()
    already = {(row[0], row[1]) for row in existing}

    now = time.time()
    insert = sa.text(
        "INSERT INTO workshopaibinding "
        "(user_id, device_id, ai_config_id, created_at, updated_at) "
        "VALUES (:user_id, :device_id, :ai_config_id, :created_at, :updated_at)"
    )
    for cfg_id, user_id in configs:
        if user_id is None or cfg_id is None:
            continue
        if (user_id, cfg_id) in already:
            continue
        bind.execute(
            insert,
            {
                "user_id": user_id,
                "device_id": f"toolbox_builtin_{user_id}",
                "ai_config_id": cfg_id,
                "created_at": now,
                "updated_at": now,
            },
        )


def downgrade() -> None:
    bind = op.get_bind()
    tables = set(inspect(bind).get_table_names())
    if "workshopaibinding" not in tables:
        return
    bind.execute(
        sa.text("DELETE FROM workshopaibinding WHERE device_id LIKE 'toolbox_builtin_%'")
    )
