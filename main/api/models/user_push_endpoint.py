"""User-owned device endpoints for operating-system push providers."""

import time

from sqlalchemy import UniqueConstraint
from sqlmodel import Field, SQLModel


class UserPushEndpoint(SQLModel, table=True):
    __table_args__ = (
        UniqueConstraint("provider", "device_id", name="uq_userpushendpoint_provider_device"),
    )

    id: str = Field(primary_key=True)
    user_id: int = Field(foreign_key="user.id", index=True)
    provider: str = Field(index=True)
    device_id: str = Field(index=True)
    push_token: str
    app_version: str = ""
    enabled: bool = Field(default=True, index=True)
    created_at: float = Field(default_factory=time.time, index=True)
    updated_at: float = Field(default_factory=time.time, index=True)
    last_seen_at: float = Field(default_factory=time.time, index=True)
