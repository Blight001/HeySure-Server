"""Process-local MCP run context propagated across async/thread boundaries."""

from __future__ import annotations

import contextvars
from typing import Any, Dict, Optional


_RUN_SESSION_CONTEXT: contextvars.ContextVar[Optional[Dict[str, Any]]] = contextvars.ContextVar(
    "run_session_context", default=None
)


def set_run_session_context(ctx: Optional[Dict[str, Any]]):
    return _RUN_SESSION_CONTEXT.set(ctx or None)


def reset_run_session_context(token) -> None:
    _RUN_SESSION_CONTEXT.reset(token)


def get_run_session_context() -> Optional[Dict[str, Any]]:
    return _RUN_SESSION_CONTEXT.get()


def enrich_bot_scope(session, context: Dict[str, Any]) -> Dict[str, Any]:
    """Add opaque bot ownership to a run context when its session has a route."""
    ai_config_id = context.get("ai_config_id")
    if ai_config_id is None or not callable(getattr(session, "exec", None)):
        return context
    from sqlmodel import select
    from api.models import BotConnection, BotContact, BotSessionRoute

    route = session.exec(select(BotSessionRoute).where(
        BotSessionRoute.user_id == context.get("user_id"),
        BotSessionRoute.ai_config_id == ai_config_id,
        BotSessionRoute.ai_kind == context.get("ai_kind"),
        BotSessionRoute.session_id == context.get("session_id"),
    )).first()
    if route is None:
        return context
    connection = session.get(BotConnection, route.connection_id) if route.connection_id else None
    contact = session.get(BotContact, route.contact_id) if route.contact_id else None
    context.update({
        "bot_channel": route.channel,
        "connection_ref": connection.connection_ref if connection else "",
        "recipient_ref": contact.contact_ref if contact else "",
        "bot_connection_id": route.connection_id,
        "bot_contact_id": route.contact_id,
    })
    return context
