"""move MCP authorization from roles/configs to per-device member scopes

Revision ID: a63b9d4e2f71
Revises: f52a8c3d1e40
Create Date: 2026-08-14
"""

import json
import time
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op


revision: str = "a63b9d4e2f71"
down_revision: Union[str, Sequence[str], None] = "f52a8c3d1e40"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

_LIBRARY_TOOLS = {"member.manage", "device+mcp.manage", "knowledge.manage"}


def _decode_tools(raw: object) -> set[str]:
    try:
        value = json.loads(str(raw or "[]"))
    except Exception:
        return set()
    if not isinstance(value, list):
        return set()
    return {str(item).strip() for item in value if isinstance(item, str) and str(item).strip()}


def upgrade() -> None:
    connection = op.get_bind()
    rows = connection.execute(sa.text("""
        SELECT b.user_id, b.device_id, b.ai_config_id, c.mcp_tools
        FROM workshopaibinding AS b
        JOIN assistantaiconfig AS c
          ON c.id = b.ai_config_id AND c.user_id = b.user_id
        WHERE b.device_id LIKE 'workshop_builtin_%'
    """)).mappings()
    now = time.time()
    for row in rows:
        selected = sorted(_decode_tools(row["mcp_tools"]) & _LIBRARY_TOOLS)
        connection.execute(
            sa.text("""
                INSERT INTO devicetypemcppermission
                    (user_id, device_id, ai_config_id, device_type, tools_json, created_at, updated_at)
                VALUES
                    (:user_id, :device_id, :ai_config_id, 'workshop', :tools_json, :now, :now)
                ON CONFLICT (user_id, device_id, ai_config_id)
                    WHERE ai_config_id IS NOT NULL
                DO UPDATE SET
                    tools_json = EXCLUDED.tools_json,
                    device_type = EXCLUDED.device_type,
                    updated_at = EXCLUDED.updated_at
            """),
            {
                "user_id": int(row["user_id"]),
                "device_id": str(row["device_id"]),
                "ai_config_id": int(row["ai_config_id"]),
                "tools_json": json.dumps(selected, ensure_ascii=False),
                "now": now,
            },
        )

    # Keep the physical column for one rolling-release compatibility window,
    # but remove every effective legacy policy before new runtimes start.
    connection.execute(sa.text('UPDATE "user" SET role_mcp_permissions = \'\''))


def downgrade() -> None:
    # The previous JSON role policies cannot be reconstructed after removal.
    # Device scopes remain valid and are safe for an older application to ignore.
    pass
