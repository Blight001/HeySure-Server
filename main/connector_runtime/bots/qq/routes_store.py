"""QQ-specific reads/writes against the unified ``BotSessionRoute`` table.

Channel-specific addressing (``target_id`` / ``target_type``) is stored
in ``target_json``; QQ-only reply bookkeeping (``source_message_id`` /
``source_event_id`` / ``next_msg_seq``) lives in dedicated columns so the
adapter can bump ``msg_seq`` atomically per outbound reply.

``load_qq_route`` returns the live ``BotSessionRoute`` row (so the adapter
can mutate ``next_msg_seq``) plus a parsed ``target`` dict for convenience.
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass
from typing import Any, Dict, TYPE_CHECKING, Optional

from sqlmodel import select
from fastapi import HTTPException

from api.models import BotConnection, BotSessionRoute
from api.services.bot_directory import attach_route_contact
from ..connection_scope import resolve_inbound_config, scoped_home_session, scoped_identity
from ._config import QQ_DEFAULTS, read_qq_config

if TYPE_CHECKING:
    from sqlmodel import Session

    from api.models import ChatMessage


CHANNEL = "qq"


def scope_qq_inbound(session, cfg, connection_ref: str, read_config=read_qq_config):
    cfg, connection_ref = resolve_inbound_config(
        session, cfg, channel="qq", connection_ref=connection_ref, defaults=QQ_DEFAULTS,
    )
    if not read_config(cfg).get("enabled"):
        raise HTTPException(status_code=400, detail="QQ bot is disabled for this AI")
    return cfg, connection_ref


def qq_scope_keys(connection_ref: str, config_id: int, target_id: str, session_key: str):
    return scoped_identity(connection_ref, target_id), scoped_home_session(
        "qq", config_id, connection_ref, session_key,
    )


def send_qq_text_safely(**values) -> bool:
    from .service import send_qq_text_message

    body = str(values.pop("text", "") or "").strip()
    if not body:
        return False
    try:
        send_qq_text_message(text=body, **values)
        return True
    except Exception:
        return False


def qq_session_name(existing_session: Any, session_key: str) -> str:
    name = str(getattr(existing_session, "session_name", "") or "").strip()
    return name or f"QQ对话 {session_key}"


@dataclass
class QQRouteHandle:
    """Bundle of ``(row, target_id, target_type)`` returned to the adapter.

    ``row`` is the live ``BotSessionRoute`` instance — mutate ``next_msg_seq``
    on it and the caller commits inside the same session that loaded it.
    """

    row: BotSessionRoute
    target_id: str
    target_type: str
    connection_ref: str = ""

    @property
    def source_message_id(self) -> str:
        return str(self.row.source_message_id or "")

    @property
    def source_event_id(self) -> str:
        return str(self.row.source_event_id or "")

    @property
    def next_msg_seq(self) -> int:
        return int(self.row.next_msg_seq or 1)


@dataclass(frozen=True)
class QQBoundTarget:
    """A QQ recipient learned from an inbound, already-bound conversation."""

    target_id: str
    target_type: str
    session_id: str
    connection_ref: str = ""


def _decode_target(row: BotSessionRoute) -> tuple[str, str]:
    try:
        target = json.loads(row.target_json or "{}")
    except Exception:
        target = {}
    return (
        str(target.get("target_id", "") or ""),
        str(target.get("target_type", "c2c") or "c2c"),
    )


def _bind_qq_contact(session: "Session", row: BotSessionRoute, target_id: str, target_type: str, connection_ref: str) -> None:
    if connection_ref:
        connection = session.exec(select(BotConnection).where(
            BotConnection.connection_ref == connection_ref,
            BotConnection.ai_config_id == int(row.ai_config_id),
            BotConnection.channel == CHANNEL,
        )).first()
        if connection:
            row.connection_id = connection.id
    session.add(row)
    session.flush()
    attach_route_contact(
        session, route=row, identity_key=target_id,
        target={"target_id": target_id, "target_type": target_type},
    )


def _upsert_qq_route(row, **values):
    if row is None:
        return BotSessionRoute(
            channel=CHANNEL, **values,
        )
    row.target_json = values["target_json"]
    row.source_message_id = values["source_message_id"]
    row.source_event_id = values["source_event_id"]
    row.next_msg_seq = values["next_msg_seq"]
    row.updated_at = time.time()
    return row


def register_qq_session_route(
    session: "Session",
    *,
    user_id: int,
    ai_config_id: int,
    ai_kind: str,
    session_id: str,
    target_id: str,
    target_type: str,
    source_message_id: str = "",
    source_event_id: str = "",
    **options,
) -> None:
    connection_ref = str(options.get("connection_ref") or "")
    next_msg_seq = int(options.get("next_msg_seq") or 1)
    session_id = str(session_id or "").strip()
    target_id = str(target_id or "").strip()
    target_type = str(target_type or "c2c").strip() or "c2c"
    if not session_id or not target_id:
        return
    row = session.exec(
        select(BotSessionRoute).where(
            BotSessionRoute.channel == CHANNEL,
            BotSessionRoute.user_id == int(user_id),
            BotSessionRoute.ai_config_id == int(ai_config_id),
            BotSessionRoute.ai_kind == str(ai_kind or "core"),
            BotSessionRoute.session_id == session_id,
        )
    ).first()
    target_json = json.dumps(
        {"target_id": target_id, "target_type": target_type},
        ensure_ascii=False,
    )
    row = _upsert_qq_route(
        row, user_id=int(user_id), ai_config_id=int(ai_config_id),
        ai_kind=str(ai_kind or "core"), session_id=session_id, target_json=target_json,
        source_message_id=str(source_message_id or ""),
        source_event_id=str(source_event_id or ""), next_msg_seq=max(1, next_msg_seq),
    )
    _bind_qq_contact(session, row, target_id, target_type, connection_ref)
    session.commit()


def load_qq_route(
    session: "Session", message: "ChatMessage"
) -> Optional[QQRouteHandle]:
    if not message.ai_config_id:
        return None
    row = session.exec(
        select(BotSessionRoute).where(
            BotSessionRoute.channel == CHANNEL,
            BotSessionRoute.user_id == int(message.user_id),
            BotSessionRoute.ai_config_id == int(message.ai_config_id),
            BotSessionRoute.ai_kind == str(message.ai_kind or "core"),
            BotSessionRoute.session_id == str(message.session_id or ""),
        )
    ).first()
    if row is None:
        return None
    target_id, target_type = _decode_target(row)
    connection = session.get(BotConnection, row.connection_id) if row.connection_id else None
    return QQRouteHandle(
        row=row, target_id=target_id, target_type=target_type,
        connection_ref=connection.connection_ref if connection else "",
    )


def find_qq_bound_target(
    session: "Session",
    *,
    user_id: int,
    ai_config_id: int,
    ai_kind: str,
    session_id: str = "",
) -> Optional[QQBoundTarget]:
    """Resolve a recipient previously learned from QQ inbound traffic.

    When ``session_id`` is present the lookup is intentionally exact: a tool
    running inside a QQ-owned conversation must notify that same identity.
    With no session id, the most recently refreshed QQ route is used; this is
    the fallback needed by web/background runs that have no bot session of
    their own.
    """
    stmt = select(BotSessionRoute).where(
        BotSessionRoute.channel == CHANNEL,
        BotSessionRoute.user_id == int(user_id),
        BotSessionRoute.ai_config_id == int(ai_config_id),
        BotSessionRoute.ai_kind == str(ai_kind or "core"),
    )
    wanted_session_id = str(session_id or "").strip()
    if wanted_session_id:
        stmt = stmt.where(BotSessionRoute.session_id == wanted_session_id)
    rows = session.exec(stmt.order_by(BotSessionRoute.updated_at.desc())).all()
    for row in rows:
        target_id, target_type = _decode_target(row)
        if target_id:
            connection_id = getattr(row, "connection_id", None)
            connection = session.get(BotConnection, connection_id) if connection_id and hasattr(session, "get") else None
            return QQBoundTarget(
                target_id=target_id,
                target_type=target_type or "c2c",
                session_id=str(row.session_id or ""),
                connection_ref=connection.connection_ref if connection else "",
            )
    return None
