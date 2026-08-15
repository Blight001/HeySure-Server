"""Durable work orders for the first-party Codex maintenance device."""

import time
from typing import Optional

from sqlalchemy import Column, Text, UniqueConstraint
from sqlmodel import Field, SQLModel


class MaintenanceTask(SQLModel, table=True):
    task_id: str = Field(primary_key=True)
    run_id: str = Field(index=True, unique=True)
    user_id: int = Field(foreign_key="user.id", index=True)
    maintainer_ai_config_id: int = Field(foreign_key="assistantaiconfig.id", index=True)
    reporter_ai_config_id: Optional[int] = Field(
        default=None, foreign_key="assistantaiconfig.id", index=True
    )
    source_session_id: str = Field(default="", index=True)
    device_id: str = Field(index=True)
    title: str = Field(default="")
    description: str = Field(default="", sa_column=Column(Text, nullable=False))
    acceptance_criteria: str = Field(default="", sa_column=Column(Text, nullable=False))
    affected_repo: str = Field(default="", index=True)
    branch_name: str = Field(default="", index=True)
    base_sha: str = Field(default="", index=True)
    severity: str = Field(default="normal", index=True)
    dedupe_key: str = Field(default="", index=True)
    status: str = Field(default="queued", index=True)
    phase: str = Field(default="triage", index=True)
    owner: str = Field(default="")
    lease_expires_at: Optional[float] = Field(default=None, index=True)
    deadline_at: Optional[float] = Field(default=None, index=True)
    last_sequence: int = Field(default=0)
    last_device_sequence: int = Field(default=0)
    summary: str = Field(default="", sa_column=Column(Text, nullable=False))
    error_code: str = Field(default="")
    created_at: float = Field(default_factory=time.time, index=True)
    updated_at: float = Field(default_factory=time.time, index=True)
    started_at: Optional[float] = None
    finished_at: Optional[float] = None


class MaintenanceEvent(SQLModel, table=True):
    __table_args__ = (
        UniqueConstraint("run_id", "event_id", name="uq_maintenanceevent_run_event"),
        UniqueConstraint("run_id", "sequence", name="uq_maintenanceevent_run_sequence"),
    )

    id: Optional[int] = Field(default=None, primary_key=True)
    task_id: str = Field(foreign_key="maintenancetask.task_id", index=True)
    run_id: str = Field(index=True)
    event_id: str = Field(index=True)
    sequence: int = Field(index=True)
    event_type: str = Field(index=True)
    actor_type: str = Field(default="system", index=True)
    actor_id: str = Field(default="")
    phase: str = Field(default="", index=True)
    status: str = Field(default="", index=True)
    payload_json: str = Field(default="{}", sa_column=Column(Text, nullable=False))
    created_at: float = Field(default_factory=time.time, index=True)


class MaintenanceApproval(SQLModel, table=True):
    approval_id: str = Field(primary_key=True)
    task_id: str = Field(foreign_key="maintenancetask.task_id", index=True)
    run_id: str = Field(index=True)
    user_id: int = Field(foreign_key="user.id", index=True)
    request_event_id: str = Field(index=True)
    approval_type: str = Field(index=True)
    title: str = Field(default="")
    detail_json: str = Field(default="{}", sa_column=Column(Text, nullable=False))
    status: str = Field(default="pending", index=True)
    decision: str = Field(default="")
    decided_by_user_id: Optional[int] = Field(default=None, foreign_key="user.id")
    created_at: float = Field(default_factory=time.time, index=True)
    expires_at: Optional[float] = Field(default=None, index=True)
    decided_at: Optional[float] = None
