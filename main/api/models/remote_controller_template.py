"""Durable per-user remote controller templates and built-in overrides."""

import time
from typing import Optional

from sqlalchemy import Column, Text, UniqueConstraint
from sqlmodel import Field, SQLModel


class RemoteControllerTemplate(SQLModel, table=True):
    """User-owned controller template or override of a built-in template."""

    __table_args__ = (
        UniqueConstraint(
            "user_id",
            "template_id",
            name="uq_remote_controller_template_user_template",
        ),
    )

    id: str = Field(primary_key=True)
    user_id: int = Field(foreign_key="user.id", index=True)
    template_id: str = Field(index=True)
    document_json: str = Field(sa_column=Column(Text, nullable=False))
    revision: int = Field(default=1)
    builtin_override: bool = Field(default=False, index=True)
    created_at: float = Field(default_factory=time.time)
    updated_at: float = Field(default_factory=time.time, index=True)
    deleted_at: Optional[float] = Field(default=None, index=True)
