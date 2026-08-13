"""Unified session-route table covering every bot channel.

Replaces the per-bot ``FeishuSessionRoute`` / ``QQSessionRoute`` tables.
A single row binds ``(channel, user, ai_config, ai_kind, session_id)`` to a
JSON-encoded addressing payload (``target_json``) plus a few QQ-specific
columns kept hot for atomic ``msg_seq`` updates.

Adding a new bot does not require a new table — the adapter just stores
its addressing payload under its own ``channel`` value.
"""

import time
from typing import Optional

from sqlalchemy import UniqueConstraint
from sqlmodel import Field, SQLModel


class BotConnection(SQLModel, table=True):
    """Encrypted credentials and cursor for a stateful bot account."""

    id: Optional[int] = Field(default=None, primary_key=True)
    connection_ref: str = Field(default="", index=True, unique=True)
    name: str = Field(default="")
    enabled: bool = Field(default=True, index=True)
    is_default: bool = Field(default=False)
    user_id: int = Field(foreign_key="user.id", index=True)
    ai_config_id: int = Field(foreign_key="assistantaiconfig.id", index=True)
    channel: str = Field(index=True)
    provider: str = Field(default="")
    provider_account_id: str = Field(default="", index=True)
    owner_external_id: str = Field(default="")
    base_url: str = Field(default="")
    credentials_encrypted: str = Field(default="")
    sync_cursor: str = Field(default="")
    state: str = Field(default="disconnected", index=True)
    last_error_code: str = Field(default="")
    last_seen_at: float = Field(default=0.0)
    created_at: float = Field(default_factory=time.time)
    updated_at: float = Field(default_factory=time.time)


class BotContact(SQLModel, table=True):
    """An external recipient known by a bot connection.

    Only opaque refs are exposed to models. Provider ids and addressing data
    are stored as a hash/encrypted envelope respectively.
    """

    __table_args__ = (
        UniqueConstraint("connection_id", "external_id_hash", name="uq_bot_contact_connection_identity"),
    )

    id: Optional[int] = Field(default=None, primary_key=True)
    connection_id: int = Field(foreign_key="botconnection.id", index=True)
    user_id: int = Field(foreign_key="user.id", index=True)
    ai_config_id: int = Field(foreign_key="assistantaiconfig.id", index=True)
    contact_ref: str = Field(default="", index=True, unique=True)
    external_id_hash: str = Field(default="", index=True)
    display_name: str = Field(default="")
    target_encrypted: str = Field(default="")
    enabled: bool = Field(default=True, index=True)
    allow_proactive: bool = Field(default=True)
    last_seen_at: float = Field(default=0.0)
    created_at: float = Field(default_factory=time.time)
    updated_at: float = Field(default_factory=time.time)


class BotSessionRoute(SQLModel, table=True):
    id: Optional[int] = Field(default=None, primary_key=True)
    channel: str = Field(index=True)
    user_id: int = Field(foreign_key="user.id", index=True)
    ai_config_id: int = Field(foreign_key="assistantaiconfig.id", index=True)
    ai_kind: str = Field(default="core", index=True)
    session_id: str = Field(index=True)
    connection_id: Optional[int] = Field(default=None, foreign_key="botconnection.id", index=True)
    contact_id: Optional[int] = Field(default=None, foreign_key="botcontact.id", index=True)
    # JSON-encoded bot-specific addressing payload, e.g.
    #   Feishu: {"receive_id": "...", "receive_id_type": "..."}
    #   QQ:     {"target_id": "...", "target_type": "..."}
    target_json: str = Field(default="{}")
    # QQ requires the source message id + an in-order msg_seq for each
    # outbound reply; kept as columns so we can bump them atomically without
    # parsing target_json on every send. Feishu rows leave them empty.
    source_message_id: str = Field(default="")
    source_event_id: str = Field(default="")
    next_msg_seq: int = Field(default=1)
    created_at: float = Field(default_factory=time.time)
    updated_at: float = Field(default_factory=time.time)


class BotUserCursor(SQLModel, table=True):
    """Per-contact active-session pointer and conversation isolation scope.

    The contact id is the privacy boundary used by conversation list/switch;
    the channel identity key remains only an adapter-side lookup key. Logical uniqueness:
    ``(channel, ai_config_id, ai_kind, identity_key)`` — upserted on read/write.
    """

    id: Optional[int] = Field(default=None, primary_key=True)
    channel: str = Field(index=True)
    user_id: int = Field(foreign_key="user.id", index=True)  # AI owner
    ai_config_id: int = Field(foreign_key="assistantaiconfig.id", index=True)
    ai_kind: str = Field(default="core", index=True)
    # Channel-agnostic identity key (QQ openid / Feishu receive_id / …),
    # supplied by each adapter's ``route_identity_key``.
    identity_key: str = Field(index=True)
    contact_id: Optional[int] = Field(default=None, foreign_key="botcontact.id", index=True)
    # The session this identity's next inbound message will be routed to.
    active_session_id: str = Field(default="")
    created_at: float = Field(default_factory=time.time)
    updated_at: float = Field(default_factory=time.time)
