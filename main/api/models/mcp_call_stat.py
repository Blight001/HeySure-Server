"""MCP call statistics + failure trail, so an AI can trace and fix flaky tools.

``McpToolStat`` is a per-(user, ai_config, tool) running counter used to compute
a failure rate. ``McpFailureEvent`` records each failure with the conversation
location (session / run / message) where it happened, so the failure can be
located in chat and the responsible tool iterated.
"""

import time
from typing import Optional

from sqlalchemy import Index
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


class McpProgramRun(SQLModel, table=True):
    """Durable owner-scoped execution record for asynchronous MCP programs."""

    __table_args__ = (
        Index("ix_mcpprogramrun_user_status", "user_id", "status"),
        Index("ix_mcpprogramrun_status_lease", "status", "lease_until"),
    )

    id: str = Field(primary_key=True)
    user_id: int = Field(foreign_key="user.id", index=True)
    ai_config_id: Optional[int] = Field(default=None, index=True)
    status: str = Field(default="queued", index=True)
    # Program, input and context are encrypted envelopes; results are encrypted
    # too because a successful tool can contain user content or credentials.
    program_json: str
    input_json: str = Field(default="")
    allowed_tools_json: str = Field(default="[]")
    session_context_json: str = Field(default="")
    output_json: Optional[str] = None
    error_json: Optional[str] = None
    owner: str = Field(default="", index=True)
    lease_until: Optional[float] = Field(default=None, index=True)
    deadline_at: float = Field(index=True)
    cancel_requested: bool = Field(default=False, index=True)
    created_at: float = Field(default_factory=time.time, index=True)
    updated_at: float = Field(default_factory=time.time, index=True)
    started_at: Optional[float] = None
    finished_at: Optional[float] = None
