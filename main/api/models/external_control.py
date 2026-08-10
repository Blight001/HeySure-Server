"""Persistent remote-MCP controller credentials and execution journal."""

import time
from typing import Optional

from sqlmodel import Field, SQLModel


class ExternalControllerCredential(SQLModel, table=True):
    id: Optional[int] = Field(default=None, primary_key=True)
    user_id: int = Field(foreign_key="user.id", index=True)
    ai_config_id: int = Field(foreign_key="assistantaiconfig.id", index=True)
    token_hash: str = Field(index=True, unique=True)
    token_prefix: str = Field(default="")
    label: str = Field(default="Codex")
    state: str = Field(default="active", index=True)  # active / revoked / expired
    scopes_json: str = Field(default='["context:read","mcp:call","run:write","audit:read"]')
    created_at: float = Field(default_factory=time.time)
    expires_at: float = Field(index=True)
    last_seen_at: Optional[float] = Field(default=None, index=True)
    revoked_at: Optional[float] = None


class ExternalControllerRun(SQLModel, table=True):
    run_id: str = Field(primary_key=True)
    user_id: int = Field(foreign_key="user.id", index=True)
    ai_config_id: int = Field(foreign_key="assistantaiconfig.id", index=True)
    credential_id: int = Field(foreign_key="externalcontrollercredential.id", index=True)
    status: str = Field(default="queued", index=True)
    title: str = Field(default="")
    summary: str = Field(default="")
    error_message: str = Field(default="")
    lease_owner: str = Field(default="")
    lease_expires_at: Optional[float] = Field(default=None, index=True)
    created_at: float = Field(default_factory=time.time, index=True)
    updated_at: float = Field(default_factory=time.time, index=True)
    started_at: Optional[float] = None
    finished_at: Optional[float] = None


class ExternalControllerEvent(SQLModel, table=True):
    id: Optional[int] = Field(default=None, primary_key=True)
    user_id: int = Field(foreign_key="user.id", index=True)
    ai_config_id: int = Field(foreign_key="assistantaiconfig.id", index=True)
    credential_id: Optional[int] = Field(default=None, foreign_key="externalcontrollercredential.id", index=True)
    run_id: Optional[str] = Field(default=None, foreign_key="externalcontrollerrun.run_id", index=True)
    event_type: str = Field(index=True)
    tool_name: str = Field(default="", index=True)
    status: str = Field(default="ok", index=True)
    result_json: str = Field(default="{}")
    created_at: float = Field(default_factory=time.time, index=True)
