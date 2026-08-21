"""MCP call statistics + failure trail, so an AI can trace and fix flaky tools.

``McpToolStat`` is a per-(user, ai_config, tool) running counter used to compute
a failure rate. ``McpFailureEvent`` records each failure with the conversation
location (session / run / message) where it happened, so the failure can be
located in chat and the responsible tool iterated.
"""

import time
from typing import Optional

from sqlmodel import Field, SQLModel


class McpToolStat(SQLModel, table=True):
    id: Optional[int] = Field(default=None, primary_key=True)
    user_id: int = Field(foreign_key="user.id", index=True)
    ai_config_id: Optional[int] = Field(default=None, index=True)
    tool: str = Field(default="", index=True)
    total: int = Field(default=0)
    failures: int = Field(default=0)
    last_called_at: float = Field(default=0.0)
    last_failure_at: float = Field(default=0.0)
    last_error: str = Field(default="")


class McpFailureEvent(SQLModel, table=True):
    id: Optional[int] = Field(default=None, primary_key=True)
    user_id: int = Field(foreign_key="user.id", index=True)
    ai_config_id: Optional[int] = Field(default=None, index=True)
    tool: str = Field(default="", index=True)
    error: str = Field(default="")
    # Conversation location of the failure.
    session_id: str = Field(default="")
    run_id: str = Field(default="")
    message_id: Optional[int] = Field(default=None)
    created_at: float = Field(default_factory=time.time, index=True)


class ExternalMcpCredential(SQLModel, table=True):
    id: Optional[int] = Field(default=None, primary_key=True)
    user_id: int = Field(foreign_key="user.id", index=True)
    ai_config_id: int = Field(foreign_key="assistantaiconfig.id", index=True)
    token_hash: str = Field(index=True, unique=True, max_length=64)
    token_prefix: str = Field(default="", max_length=16)
    label: str = Field(default="External AI", max_length=80)
    created_at: float = Field(default_factory=time.time, index=True)
    expires_at: Optional[float] = Field(default=None, index=True)
    last_used_at: Optional[float] = Field(default=None, index=True)
    revoked_at: Optional[float] = Field(default=None, index=True)


class ExternalMcpCallAudit(SQLModel, table=True):
    """One external protocol call without arguments, results, or secrets."""

    id: Optional[int] = Field(default=None, primary_key=True)
    user_id: int = Field(foreign_key="user.id", index=True)
    ai_config_id: int = Field(foreign_key="assistantaiconfig.id", index=True)
    credential_id: Optional[int] = Field(
        default=None,
        foreign_key="externalmcpcredential.id",
        index=True,
    )
    protocol_method: str = Field(default="", index=True, max_length=40)
    tool_name: str = Field(default="", index=True, max_length=255)
    success: bool = Field(default=False, index=True)
    error_code: str = Field(default="", max_length=80)
    duration_ms: int = Field(default=0)
    created_at: float = Field(default_factory=time.time, index=True)
