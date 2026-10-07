"""Persistence models for local Codex bridge sessions and event streams."""

import time
from typing import Optional

from sqlalchemy import Column, Text
from sqlmodel import Field, SQLModel


class CodexSession(SQLModel, table=True):
    session_id: str = Field(primary_key=True)
    user_id: int = Field(foreign_key="user.id", index=True)
    device_id: str = Field(index=True)
    ai_config_id: Optional[int] = Field(default=None, index=True)
    codex_thread_id: str = Field(default="", index=True)
    model: str = Field(default="")
    cwd: str = Field(default="")
    sandbox_policy: str = Field(default="")
    approval_policy: str = Field(default="")
    status: str = Field(default="created", index=True)
    created_at: float = Field(default_factory=time.time)
    updated_at: float = Field(default_factory=time.time)


class CodexEvent(SQLModel, table=True):
    id: Optional[int] = Field(default=None, primary_key=True)
    session_id: str = Field(index=True)
    user_id: int = Field(foreign_key="user.id", index=True)
    event_type: str = Field(default="event", index=True)
    sequence: int = Field(default=0, index=True)
    payload_json: str = Field(default="{}", sa_column=Column(Text, nullable=False))
    created_at: float = Field(default_factory=time.time, index=True)

