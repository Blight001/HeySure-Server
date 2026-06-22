"""Immutable version snapshots of device dynamic MCP tools, for rollback.

Every change to a ``DeviceDynamicTool`` (whether made from the web console or by
an AI via ``device_mcp.manage``) appends one snapshot here. Operators and AIs
can list the history and restore any prior snapshot, so a tool that gets edited
into a broken state can always be rolled back.
"""

import time
from typing import Optional

from sqlmodel import Field, SQLModel


class DeviceDynamicToolVersion(SQLModel, table=True):
    id: Optional[int] = Field(default=None, primary_key=True)
    user_id: int = Field(foreign_key="user.id", index=True)
    device_type: str = Field(default="", index=True)
    name: str = Field(default="", index=True)
    # sha256 of the captured definition; lets the UI dedupe / show identity.
    revision: str = Field(default="")
    # "upsert" | "delete" | "restore" — how this snapshot came to be.
    action: str = Field(default="upsert")
    # "web" | "ai" — who made the change.
    actor: str = Field(default="web")
    # The AI that made the change, when actor == "ai".
    ai_config_id: Optional[int] = Field(default=None, index=True)
    # Full snapshot of the tool definition at this point in time.
    description: str = Field(default="")
    input_schema_json: str = Field(default="{}")
    code_kind: str = Field(default="program")
    code_json: str = Field(default="[]")
    js_source: str = Field(default="")
    runtime: str = Field(default="")
    source: str = Field(default="")
    permissions_json: str = Field(default="[]")
    created_at: float = Field(default_factory=time.time, index=True)
