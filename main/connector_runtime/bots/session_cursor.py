"""Shared "active session" cursor for the unified bot conversation pool.

Each external contact owns an isolated conversation pool while the account
owner's web view may inspect every pool. This module records which session a
given inbound identity's next message should land in.

Design notes:
- **Contact isolation.** Bot callers can list/switch only sessions carrying
  their ``bot_contact_id``. Owner web calls omit that scope and retain full view.
- **Channel-agnostic.** Helpers take a generic ``identity_key`` (QQ openid /
  Feishu receive_id / …). Routers compute it inline from their own event shape;
  the reverse direction (MCP tool → identity) decodes it from the stored
  ``BotSessionRoute.target_json``.
"""

from __future__ import annotations

import json
import time
from typing import TYPE_CHECKING, Any, Dict, List, Optional, Tuple

from sqlmodel import select

from api.models import BotConnection, BotSessionRoute, BotUserCursor, ChatSession

if TYPE_CHECKING:
    from sqlmodel import Session


def _identity_from_target(channel: str, target_json: str) -> str:
    """Decode the identity key from a stored addressing payload.

    QQ stores ``{"target_id": ...}``; Feishu stores ``{"receive_id": ...}``.
    Fall back across both so a new-but-similar channel still resolves.
    """
    try:
        target = json.loads(target_json or "{}")
    except Exception:
        target = {}
    return str(
        target.get("target_id")
        or target.get("receive_id")
        or target.get("open_id")
        or target.get("chat_id")
        or ""
    ).strip()


def _get_cursor_row(
    session: "Session",
    *,
    channel: str,
    user_id: int,
    ai_config_id: int,
    ai_kind: str,
    identity_key: str,
) -> Optional[BotUserCursor]:
    return session.exec(
        select(BotUserCursor).where(
            BotUserCursor.channel == channel,
            BotUserCursor.user_id == int(user_id),
            BotUserCursor.ai_config_id == int(ai_config_id),
            BotUserCursor.ai_kind == str(ai_kind or "core"),
            BotUserCursor.identity_key == str(identity_key),
        )
    ).first()


def _session_exists(
    session: "Session",
    *,
    user_id: int,
    ai_config_id: int,
    ai_kind: str,
    session_id: str,
    contact_id: Optional[int] = None,
) -> bool:
    stmt = select(ChatSession).where(
            ChatSession.user_id == int(user_id),
            ChatSession.ai_config_id == int(ai_config_id),
            ChatSession.ai_kind == str(ai_kind or "core"),
            ChatSession.session_id == str(session_id),
        )
    if contact_id is not None:
        stmt = stmt.where(ChatSession.bot_contact_id == int(contact_id))
    row = session.exec(stmt).first()
    return row is not None


def get_active_session_id(
    session: "Session",
    *,
    channel: str,
    user_id: int,
    ai_config_id: int,
    ai_kind: str,
    identity_key: str,
    default: str,
) -> str:
    """Return the session this identity's next inbound message should use.

    Falls back to ``default`` (the identity's home session) when there is no
    cursor yet, or when the cursor points at a session that no longer exists
    (in which case the stale cursor is reset to ``default``).
    """
    identity_key = str(identity_key or "").strip()
    default = str(default or "").strip()
    if not identity_key:
        return default
    cur = _get_cursor_row(
        session,
        channel=channel,
        user_id=user_id,
        ai_config_id=ai_config_id,
        ai_kind=ai_kind,
        identity_key=identity_key,
    )
    if cur and str(cur.active_session_id or "").strip():
        if cur.contact_id is None and default:
            home_route = session.exec(select(BotSessionRoute).where(
                BotSessionRoute.channel == str(channel),
                BotSessionRoute.user_id == int(user_id),
                BotSessionRoute.ai_config_id == int(ai_config_id),
                BotSessionRoute.ai_kind == str(ai_kind or "core"),
                BotSessionRoute.session_id == default,
            )).first()
            if home_route and home_route.contact_id:
                cur.contact_id = home_route.contact_id
                session.add(cur)
                session.commit()
        active = str(cur.active_session_id).strip()
        if _session_exists(
            session,
            user_id=user_id,
            ai_config_id=ai_config_id,
            ai_kind=ai_kind,
            session_id=active,
            contact_id=cur.contact_id,
        ):
            return active
        # Stale pointer (session was deleted) -> reset to home.
        cur.active_session_id = default
        cur.updated_at = time.time()
        session.add(cur)
        session.commit()
        return default
    return default


def set_active_session_id(
    session: "Session",
    *,
    channel: str,
    user_id: int,
    ai_config_id: int,
    ai_kind: str,
    identity_key: str,
    session_id: str,
) -> None:
    """Upsert the cursor so this identity's next message lands in ``session_id``."""
    identity_key = str(identity_key or "").strip()
    session_id = str(session_id or "").strip()
    if not identity_key or not session_id:
        return
    cur = _get_cursor_row(
        session,
        channel=channel,
        user_id=user_id,
        ai_config_id=ai_config_id,
        ai_kind=ai_kind,
        identity_key=identity_key,
    )
    now = time.time()
    if cur is None:
        route = session.exec(select(BotSessionRoute).where(
            BotSessionRoute.channel == str(channel),
            BotSessionRoute.user_id == int(user_id),
            BotSessionRoute.ai_config_id == int(ai_config_id),
            BotSessionRoute.ai_kind == str(ai_kind or "core"),
            BotSessionRoute.session_id == session_id,
        )).first()
        cur = BotUserCursor(
            channel=str(channel),
            user_id=int(user_id),
            ai_config_id=int(ai_config_id),
            ai_kind=str(ai_kind or "core"),
            identity_key=identity_key,
            contact_id=route.contact_id if route else None,
            active_session_id=session_id,
            created_at=now,
            updated_at=now,
        )
    else:
        route = session.exec(select(BotSessionRoute).where(
            BotSessionRoute.channel == str(channel),
            BotSessionRoute.user_id == int(user_id),
            BotSessionRoute.ai_config_id == int(ai_config_id),
            BotSessionRoute.ai_kind == str(ai_kind or "core"),
            BotSessionRoute.session_id == session_id,
        )).first()
        if route and route.contact_id:
            cur.contact_id = route.contact_id
        cur.active_session_id = session_id
        cur.updated_at = now
    session.add(cur)
    session.commit()


def resolve_identity_for_session(
    session: "Session",
    *,
    user_id: int,
    ai_config_id: int,
    ai_kind: str,
    session_id: str,
) -> Optional[Tuple[str, str]]:
    """Reverse-lookup ``(channel, identity_key)`` for a bot session id.

    Returns ``None`` for sessions that have no bot route (e.g. web-only
    sessions) — the caller then has no cursor to move.
    """
    row = session.exec(
        select(BotSessionRoute).where(
            BotSessionRoute.user_id == int(user_id),
            BotSessionRoute.ai_config_id == int(ai_config_id),
            BotSessionRoute.ai_kind == str(ai_kind or "core"),
            BotSessionRoute.session_id == str(session_id),
        )
    ).first()
    if row is None:
        return None
    identity_key = _identity_from_target(row.channel, row.target_json)
    if not identity_key:
        return None
    connection = session.get(BotConnection, row.connection_id) if row.connection_id else None
    if connection and connection.connection_ref:
        identity_key = f"{connection.connection_ref}:{identity_key}"
    return (str(row.channel), identity_key)


def resolve_route_scope_for_session(
    session: "Session",
    *,
    user_id: int,
    ai_config_id: int,
    ai_kind: str,
    session_id: str,
) -> Optional[BotSessionRoute]:
    return session.exec(select(BotSessionRoute).where(
        BotSessionRoute.user_id == int(user_id),
        BotSessionRoute.ai_config_id == int(ai_config_id),
        BotSessionRoute.ai_kind == str(ai_kind or "core"),
        BotSessionRoute.session_id == str(session_id),
    )).first()


def list_ai_sessions(
    session: "Session",
    *,
    user_id: int,
    ai_config_id: Optional[int],
    ai_kind: str,
    active_session_id: str = "",
    limit: int = 50,
    bot_contact_id: Optional[int] = None,
) -> List[Dict[str, Any]]:
    """List owner-visible sessions or one external contact's isolated pool."""
    stmt = select(ChatSession).where(
        ChatSession.user_id == int(user_id),
        ChatSession.ai_kind == str(ai_kind or "core"),
    )
    if ai_config_id is not None:
        stmt = stmt.where(ChatSession.ai_config_id == int(ai_config_id))
    else:
        stmt = stmt.where(ChatSession.ai_config_id.is_(None))
    if bot_contact_id is not None:
        stmt = stmt.where(ChatSession.bot_contact_id == int(bot_contact_id))
    rows = session.exec(stmt.order_by(ChatSession.updated_at.desc())).all()

    # Tag each session with its originating channel (web when no route).
    route_stmt = select(BotSessionRoute).where(
        BotSessionRoute.user_id == int(user_id),
        BotSessionRoute.ai_kind == str(ai_kind or "core"),
    )
    if ai_config_id is not None:
        route_stmt = route_stmt.where(BotSessionRoute.ai_config_id == int(ai_config_id))
    if bot_contact_id is not None:
        route_stmt = route_stmt.where(BotSessionRoute.contact_id == int(bot_contact_id))
    channel_by_sid: Dict[str, str] = {
        str(r.session_id): str(r.channel) for r in session.exec(route_stmt).all()
    }

    limit = max(1, min(int(limit or 50), 200))
    out: List[Dict[str, Any]] = []
    for row in rows[:limit]:
        sid = str(row.session_id)
        out.append(
            {
                "id": row.id,
                "session_id": sid,
                "name": row.session_name,
                "source": channel_by_sid.get(sid, "web"),
                "model_preset_id": str(getattr(row, "model_preset_id", "") or ""),
                "created_at": row.created_at,
                "updated_at": row.updated_at,
                "is_active": bool(active_session_id) and sid == str(active_session_id),
            }
        )
    return out
