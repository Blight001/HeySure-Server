"""Resolve one bot-account instance without leaking provider credentials."""

from __future__ import annotations

from typing import Any, Dict, Tuple

from fastapi import HTTPException
from sqlmodel import Session, select

from api.models import BotConnection
from api.services.bot_directory import config_view_for_connection


def resolve_inbound_config(
    session: Session,
    cfg,
    *,
    channel: str,
    connection_ref: str,
    defaults: Dict[str, Any],
) -> Tuple[Any, str]:
    """Return an instance-scoped config and its stable public reference."""
    stmt = select(BotConnection).where(
        BotConnection.ai_config_id == int(cfg.id or 0),
        BotConnection.channel == channel,
        BotConnection.enabled.is_(True),
        BotConnection.state != "deleted",
    )
    if connection_ref:
        stmt = stmt.where(BotConnection.connection_ref == connection_ref)
    connection = session.exec(
        stmt.order_by(BotConnection.is_default.desc(), BotConnection.created_at.asc())
    ).first()
    if connection_ref and connection is None:
        raise HTTPException(status_code=404, detail=f"{channel} connection not found")
    if connection is None:
        return cfg, ""
    return config_view_for_connection(cfg, connection, defaults), connection.connection_ref


def scoped_identity(connection_ref: str, external_id: str) -> str:
    return f"{connection_ref}:{external_id}" if connection_ref else external_id


def scoped_home_session(channel: str, config_id: int, connection_ref: str, key: str) -> str:
    parts = (channel, str(config_id), connection_ref, key) if connection_ref else (channel, str(config_id), key)
    return "_".join(parts)
