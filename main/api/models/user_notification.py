"""Durable first-party notifications addressed to the signed-in user."""

import time
from typing import Optional

from sqlmodel import Field, SQLModel


class UserNotification(SQLModel, table=True):
    id: str = Field(primary_key=True)
    user_id: int = Field(foreign_key="user.id", index=True)
    ai_config_id: Optional[int] = Field(default=None, index=True)
    kind: str = Field(default="message", index=True)
    title: str = Field(default="")
    body: str = Field(default="")
    severity: str = Field(default="info", index=True)
    status: str = Field(default="unread", index=True)
    source: str = Field(default="message.send+to")
    action_url: str = Field(default="")
    attachments_json: str = Field(default="[]")
    app_push_required: bool = Field(default=True, index=True)
    external_channel: str = Field(default="")
    external_delivered: bool = Field(default=False)
    created_at: float = Field(default_factory=time.time, index=True)
    updated_at: float = Field(default_factory=time.time, index=True)
    read_at: Optional[float] = Field(default=None, index=True)
