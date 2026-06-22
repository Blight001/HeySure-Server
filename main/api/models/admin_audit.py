import time
from typing import Optional

from sqlmodel import Field, SQLModel


class AdminAuditLog(SQLModel, table=True):
    """Persistent record of privileged admin-panel actions.

    Console logs vanish on restart, so role changes, password resets, user
    deletions and service restarts are also written here for an auditable
    trail of who did what to whom.
    """

    id: Optional[int] = Field(default=None, primary_key=True)
    created_at: float = Field(default_factory=time.time, index=True)
    actor_id: Optional[int] = Field(default=None, index=True)
    actor_account: str = Field(default="")
    action: str = Field(index=True)  # e.g. set_role / reset_password / delete_user / restart_service / create_user / stop_task
    target_type: str = Field(default="")  # user / service / task
    target_id: str = Field(default="")
    target_label: str = Field(default="")
    detail: str = Field(default="")  # short human-readable summary
