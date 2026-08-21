"""Persistent workflow-card definitions and deterministic device runs."""

import time
from typing import ClassVar, Optional

from sqlalchemy import Index, UniqueConstraint
from sqlmodel import Field, SQLModel


class WorkflowCard(SQLModel, table=True):
    __table_args__ = (Index("ix_workflowcard_user_status", "user_id", "status"),)
    RUNNABLE_STATUSES: ClassVar[frozenset[str]] = frozenset({"active", "published", "deprecated"})
    AI_OWNER_TAG_PREFIX: ClassVar[str] = "ai_owner:"
    ACCESS_SCOPES: ClassVar[frozenset[str]] = frozenset({"all", "owner", "selected"})

    id: str = Field(primary_key=True)
    user_id: int = Field(foreign_key="user.id", index=True)
    name: str = Field(default="", index=True)
    description: str = Field(default="")
    status: str = Field(default="draft", index=True)
    risk_level: str = Field(default="read_only")
    tags_json: str = Field(default="[]")
    access_scope: str = Field(default="all", index=True)
    allowed_ai_config_ids_json: str = Field(default="[]")
    draft_definition_json: str = Field(default="{}")
    editor_layout_json: str = Field(default="{}")
    latest_version_id: Optional[str] = Field(default=None, foreign_key="workflowcardversion.id", index=True)
    created_by: int = Field(foreign_key="user.id")
    created_at: float = Field(default_factory=time.time)
    updated_at: float = Field(default_factory=time.time, index=True)
    deleted_at: Optional[float] = Field(default=None, index=True)

    @classmethod
    def is_runnable_status(cls, status: object) -> bool:
        return str(status or "") in cls.RUNNABLE_STATUSES

    @classmethod
    def ai_owner_ids(cls, tags: object) -> set[str]:
        values = tags if isinstance(tags, list) else []
        return {
            str(item).strip()[len(cls.AI_OWNER_TAG_PREFIX):]
            for item in values
            if str(item).strip().lower().startswith(cls.AI_OWNER_TAG_PREFIX)
        }

    @classmethod
    def tags_visible_to_ai(cls, tags: object, ai_config_id: Optional[int]) -> bool:
        if not ai_config_id:
            return True
        owners = cls.ai_owner_ids(tags)
        return not owners or str(ai_config_id) in owners

    @staticmethod
    def allowed_ai_config_ids(value: object) -> set[str]:
        values = value if isinstance(value, list) else []
        return {str(item).strip() for item in values if str(item).strip()}

    @classmethod
    def accessible_to_ai(
        cls,
        *,
        access_scope: object,
        allowed_ai_config_ids: object,
        tags: object,
        ai_config_id: Optional[int],
    ) -> bool:
        if not ai_config_id:
            return True
        scope = str(access_scope or "all").strip().lower()
        if scope == "owner":
            return str(ai_config_id) in cls.ai_owner_ids(tags)
        if scope == "selected":
            return str(ai_config_id) in cls.allowed_ai_config_ids(allowed_ai_config_ids)
        return True


class WorkflowCardVersion(SQLModel, table=True):
    __table_args__ = (
        UniqueConstraint("card_id", "version_number", name="uq_workflowcardversion_card_number"),
    )

    id: str = Field(primary_key=True)
    card_id: str = Field(foreign_key="workflowcard.id", index=True)
    version_number: int = Field(index=True)
    schema_version: int = Field(default=1)
    definition_json: str
    definition_digest: str = Field(index=True)
    tool_contracts_json: str = Field(default="{}")
    contract_device_ids_json: str = Field(default="[]")
    published_by: int = Field(foreign_key="user.id")
    published_at: float = Field(default_factory=time.time)


class WorkflowRun(SQLModel, table=True):
    __table_args__ = (
        UniqueConstraint("user_id", "idempotency_key", name="uq_workflowrun_user_idempotency"),
        Index("ix_workflowrun_status_wakeup", "status", "next_wakeup_at"),
        Index("ix_workflowrun_device_status", "device_id", "status"),
    )

    id: str = Field(primary_key=True)
    card_id: str = Field(foreign_key="workflowcard.id", index=True)
    card_version_id: str = Field(foreign_key="workflowcardversion.id", index=True)
    user_id: int = Field(foreign_key="user.id", index=True)
    actor_type: str = Field(default="user")
    actor_id: str = Field(default="")
    device_id: str = Field(index=True)
    status: str = Field(default="pending", index=True)
    current_step_id: str = Field(default="")
    transition_count: int = Field(default=0)
    input_json: str = Field(default="{}")
    variables_json: str = Field(default="{}")
    output_json: Optional[str] = None
    error_json: Optional[str] = None
    deadline_at: float = Field(index=True)
    next_wakeup_at: Optional[float] = Field(default=None, index=True)
    lock_version: int = Field(default=0)
    idempotency_key: str = Field(index=True)
    started_at: Optional[float] = None
    finished_at: Optional[float] = None
    created_at: float = Field(default_factory=time.time, index=True)
    updated_at: float = Field(default_factory=time.time)


class WorkflowStepRun(SQLModel, table=True):
    __table_args__ = (
        UniqueConstraint("run_id", "step_id", "attempt", name="uq_workflowsteprun_attempt"),
    )

    id: str = Field(primary_key=True)
    run_id: str = Field(foreign_key="workflowrun.id", index=True)
    step_id: str = Field(index=True)
    attempt: int = Field(default=1)
    dispatch_task_id: str = Field(unique=True, index=True)
    tool_name: str = Field(default="")
    tool_provider: str = Field(default="")
    tool_schema_digest: str = Field(default="")
    status: str = Field(default="dispatch_pending", index=True)
    claim_owner: str = Field(default="", index=True)
    claimed_at: Optional[float] = Field(default=None, index=True)
    arguments_redacted_json: str = Field(default="{}")
    arguments_json: str = Field(default="{}")
    result_projection_json: Optional[str] = None
    result_ref: Optional[str] = None
    error_json: Optional[str] = None
    started_at: Optional[float] = None
    deadline_at: float = Field(index=True)
    finished_at: Optional[float] = None


class WorkflowConfirmation(SQLModel, table=True):
    id: str = Field(primary_key=True)
    run_id: str = Field(foreign_key="workflowrun.id", index=True)
    step_id: str = Field(index=True)
    confirmation_type: str = Field(default="explicit")
    status: str = Field(default="pending", index=True)
    risk_summary: str = Field(default="")
    next_step_id: str = Field(default="")
    on_denied_step_id: str = Field(default="")
    requested_user_id: int = Field(foreign_key="user.id", index=True)
    ai_config_id: Optional[int] = Field(default=None, foreign_key="assistantaiconfig.id", index=True)
    save_as: str = Field(default="")
    response_json: Optional[str] = None
    notified_at: Optional[float] = None
    notification_run_id: str = Field(default="", index=True)
    decided_by: Optional[int] = Field(default=None, foreign_key="user.id")
    decision: Optional[str] = None
    expires_at: float = Field(index=True)
    created_at: float = Field(default_factory=time.time)
    decided_at: Optional[float] = None


class WorkflowAuditEvent(SQLModel, table=True):
    __table_args__ = (
        Index("ix_workflowauditevent_run_created", "run_id", "created_at"),
    )

    id: Optional[int] = Field(default=None, primary_key=True)
    user_id: int = Field(foreign_key="user.id", index=True)
    run_id: Optional[str] = Field(default=None, foreign_key="workflowrun.id", index=True)
    card_id: Optional[str] = Field(default=None, foreign_key="workflowcard.id", index=True)
    card_version_id: Optional[str] = Field(default=None, foreign_key="workflowcardversion.id", index=True)
    step_id: str = Field(default="", index=True)
    dispatch_task_id: str = Field(default="", index=True)
    device_id: str = Field(default="", index=True)
    event_type: str = Field(index=True)
    status_from: str = Field(default="")
    status_to: str = Field(default="")
    detail_json: str = Field(default="{}")
    created_at: float = Field(default_factory=time.time, index=True)


class WorkflowSchedulerHeartbeat(SQLModel, table=True):
    instance_id: str = Field(primary_key=True)
    heartbeat_at: float = Field(default_factory=time.time, index=True)
    last_tick_duration_ms: int = Field(default=0)
    last_error: str = Field(default="")


class WorkflowRecording(SQLModel, table=True):
    __table_args__ = (
        Index("ix_workflowrecording_owner_status", "user_id", "ai_config_id", "status"),
    )

    id: str = Field(primary_key=True)
    user_id: int = Field(foreign_key="user.id", index=True)
    ai_config_id: Optional[int] = Field(default=None, foreign_key="assistantaiconfig.id", index=True)
    name: str = Field(default="")
    description: str = Field(default="")
    status: str = Field(default="active", index=True)
    default_device_id: str = Field(default="", index=True)
    device_ids_json: str = Field(default="[]")
    event_count: int = Field(default=0)
    created_at: float = Field(default_factory=time.time, index=True)
    updated_at: float = Field(default_factory=time.time)
    stopped_at: Optional[float] = None


class WorkflowRecordingEvent(SQLModel, table=True):
    __table_args__ = (
        UniqueConstraint("recording_id", "sequence", name="uq_workflowrecordingevent_sequence"),
    )

    id: str = Field(primary_key=True)
    recording_id: str = Field(foreign_key="workflowrecording.id", index=True)
    sequence: int = Field(index=True)
    tool_name: str = Field(index=True)
    device_id: str = Field(default="", index=True)
    arguments_json: str = Field(default="{}")
    result_json: str = Field(default="{}")
    success: bool = Field(default=True)
    error: str = Field(default="")
    created_at: float = Field(default_factory=time.time, index=True)
