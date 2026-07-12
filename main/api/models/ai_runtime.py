import time
from typing import Optional

from sqlmodel import Field, SQLModel


class TokenUsageSnapshot(SQLModel, table=True):
    id: Optional[int] = Field(default=None, primary_key=True)
    user_id: int = Field(foreign_key="user.id", index=True)
    ai_config_id: Optional[int] = Field(default=None, index=True)
    ai_kind: str = Field(default="assistant", index=True)
    bucket: str = Field(index=True)  # YYYY-MM-DD
    prompt_tokens: int = Field(default=0)
    completion_tokens: int = Field(default=0)
    total_tokens: int = Field(default=0)
    updated_at: float = Field(default_factory=time.time)


class AIRuntimeStatus(SQLModel, table=True):
    id: Optional[int] = Field(default=None, primary_key=True)
    user_id: int = Field(foreign_key="user.id", index=True)
    ai_config_id: Optional[int] = Field(default=None, index=True)
    ai_kind: str = Field(default="assistant", index=True)
    running: bool = Field(default=True)
    mcp_enabled: bool = Field(default=True)
    current_status: str = Field(default="idle")
    current_mcp_tool: str = Field(default="")
    updated_at: float = Field(default_factory=time.time)


class AgentDispatchTask(SQLModel, table=True):
    """One pending/finished agent (desktop / browser plugin) tool dispatch.

    Persisted so connector-runtime restarts don't lose tasks in-flight: the
    chat_worker polls this table by ``task_id`` until ``status`` leaves
    ``pending``. ``result_json`` / ``error`` carry the agent's reply for
    consumption by the caller and for UI auditing.
    """
    id: Optional[int] = Field(default=None, primary_key=True)
    task_id: str = Field(index=True, unique=True)
    user_id: int = Field(index=True)
    ai_config_id: Optional[int] = Field(default=None, index=True)
    ai_kind: str = Field(default="assistant")
    session_id: Optional[str] = Field(default=None)
    session_name: Optional[str] = None
    device_id: str = Field(default="", index=True)
    tool: str = Field(default="")
    instruction: str = Field(default="")
    args_json: Optional[str] = None
    suppress_session_message: bool = Field(default=False)
    status: str = Field(default="pending", index=True)  # queued/pending/completed/error/timeout
    success: Optional[bool] = None
    summary: Optional[str] = None
    result_json: Optional[str] = None  # JSON-encoded payload from agent
    error: Optional[str] = None
    created_at: float = Field(default_factory=time.time, index=True)
    completed_at: Optional[float] = None


class TaskPlan(SQLModel, table=True):
    """A multi-phase execution plan an AI commits to before acting.

    The todo flow is: trigger -> todo.manage(action=create) -> execute phase by
    phase; todo.manage(action=edit) compacts and advances each phase, and editing
    the final phase automatically writes the success/failure log. One active plan
    per (user, ai_config, session).
    """
    id: Optional[int] = Field(default=None, primary_key=True)
    plan_id: str = Field(index=True, unique=True)
    user_id: int = Field(foreign_key="user.id", index=True)
    ai_config_id: int = Field(index=True)
    job_id: Optional[str] = Field(default=None, index=True)  # linked AITaskJob.job_id
    session_id: Optional[str] = Field(default=None, index=True)
    goal: str = Field(default="")
    status: str = Field(default="active", index=True)  # active/completed/failed/abandoned
    outcome: Optional[str] = None  # success/failure (set by automatic finalization)
    phase_count: int = Field(default=0)
    current_phase_seq: int = Field(default=0)  # 0-based index of the in-progress phase
    summary: Optional[str] = None  # final whole-run summary
    created_at: float = Field(default_factory=time.time, index=True)
    updated_at: float = Field(default_factory=time.time)
    finished_at: Optional[float] = None


class TaskPhase(SQLModel, table=True):
    """One phase of a :class:`TaskPlan`.

    Each phase carries its own goal and a done-signal that marks completion, and
    a JSON list of sub-actions (each with its own goal + done-signal). Phases are
    executed in ``seq`` order; ``todo.manage(action=edit)`` advances ``TaskPlan`` to
    the next one and records this phase's summary.
    """
    id: Optional[int] = Field(default=None, primary_key=True)
    phase_id: str = Field(index=True, unique=True)
    plan_id: str = Field(index=True)
    user_id: int = Field(index=True)
    seq: int = Field(default=0, index=True)  # 0-based order within the plan
    title: str = Field(default="")
    goal: str = Field(default="")
    done_signal: str = Field(default="")
    actions_json: Optional[str] = None  # JSON list of {goal, done_signal}
    status: str = Field(default="pending", index=True)  # pending/active/completed/failed
    summary: Optional[str] = None  # phase summary recorded on completion
    created_at: float = Field(default_factory=time.time)
    started_at: Optional[float] = None
    finished_at: Optional[float] = None


class AITaskJob(SQLModel, table=True):
    id: Optional[int] = Field(default=None, primary_key=True)
    job_id: str = Field(index=True, unique=True)
    user_id: int = Field(foreign_key="user.id", index=True)
    ai_config_id: int = Field(foreign_key="assistantaiconfig.id", index=True)
    created_by_ai_config_id: Optional[int] = Field(default=None, index=True)  # dispatcher's AI config id
    created_by_session_id: Optional[str] = Field(default=None, index=True)  # dispatcher's chat session id
    completion_notified_at: Optional[float] = None
    ai_kind: str = Field(default="core", index=True)
    template_id: Optional[str] = Field(default=None, index=True)
    title: str
    instruction: str
    task_payload: Optional[str] = None
    priority: int = Field(default=5, index=True)
    status: str = Field(default="queued", index=True)  # queued/running/paused/completed/cancelled
    session_id: Optional[str] = Field(default=None, index=True)
    trigger_type: str = Field(default="manual", index=True)  # manual/schedule/preempt/resume/supervision
    last_run_id: Optional[str] = Field(default=None, index=True)
    last_supervised_at: Optional[float] = None
    supervision_count: int = Field(default=0)
    created_at: float = Field(default_factory=time.time, index=True)
    updated_at: float = Field(default_factory=time.time)
    started_at: Optional[float] = None
    finished_at: Optional[float] = None
