"""Shared "active session" cursor for the unified bot conversation pool.

Every AI exposes a single shared conversation pool ("机器人对话区") that spans
the web UI and all bot channels (QQ / Feishu / future platforms). This module
records, per inbound identity, *which session that identity's next message
should land in* — and nothing more.

Design notes:
- **No isolation.** ``list_ai_sessions`` returns every session belonging to the
  AI regardless of channel or identity. ``conversation.switch`` may target any
  of them. The per-identity cursor only prevents concurrent external users from
  clobbering each other's "current session"; it is not a privacy boundary.
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

from api.models import BotSessionRoute, BotUserCursor, ChatSession

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
) -> bool:
    row = session.exec(
        select(ChatSession).where(
            ChatSession.user_id == int(user_id),
            ChatSession.ai_config_id == int(ai_config_id),
            ChatSession.ai_kind == str(ai_kind or "core"),
            ChatSession.session_id == str(session_id),
        )
    ).first()
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
        active = str(cur.active_session_id).strip()
        if _session_exists(
            session,
            user_id=user_id,
            ai_config_id=ai_config_id,
            ai_kind=ai_kind,
            session_id=active,
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
        cur = BotUserCursor(
            channel=str(channel),
            user_id=int(user_id),
            ai_config_id=int(ai_config_id),
            ai_kind=str(ai_kind or "core"),
            identity_key=identity_key,
            active_session_id=session_id,
            created_at=now,
            updated_at=now,
        )
    else:
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
    return (str(row.channel), identity_key)


def list_ai_sessions(
    session: "Session",
    *,
    user_id: int,
    ai_config_id: Optional[int],
    ai_kind: str,
    active_session_id: str = "",
    limit: int = 50,
) -> List[Dict[str, Any]]:
    """List every session in the AI's shared pool (web + all bot channels).

    No channel/identity filtering — the pool is shared. Each row is tagged with
    its source channel (``"web"`` when no bot route exists) and whether it is
    the currently active session for the asking identity.
    """
    stmt = select(ChatSession).where(
        ChatSession.user_id == int(user_id),
        ChatSession.ai_kind == str(ai_kind or "core"),
    )
    if ai_config_id is not None:
        stmt = stmt.where(ChatSession.ai_config_id == int(ai_config_id))
    else:
        stmt = stmt.where(ChatSession.ai_config_id.is_(None))
    rows = session.exec(stmt.order_by(ChatSession.updated_at.desc())).all()

    # Tag each session with its originating channel (web when no route).
    route_stmt = select(BotSessionRoute).where(
        BotSessionRoute.user_id == int(user_id),
        BotSessionRoute.ai_kind == str(ai_kind or "core"),
    )
    if ai_config_id is not None:
        route_stmt = route_stmt.where(BotSessionRoute.ai_config_id == int(ai_config_id))
    channel_by_sid: Dict[str, str] = {
        str(r.session_id): str(r.channel) for r in session.exec(route_stmt).all()
    }

    limit = max(1, min(int(limit or 50), 200))
    out: List[Dict[str, Any]] = []
    for row in rows[:limit]:
        sid = str(row.session_id)
        out.append(
            {
                "session_id": sid,
                "name": row.session_name,
                "source": channel_by_sid.get(sid, "web"),
                "created_at": row.created_at,
                "updated_at": row.updated_at,
                "is_active": bool(active_session_id) and sid == str(active_session_id),
            }
        )
    return out
