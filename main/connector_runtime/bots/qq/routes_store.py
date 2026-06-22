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
from typing import TYPE_CHECKING, Optional

from sqlmodel import select

from api.models import BotSessionRoute

if TYPE_CHECKING:
    from sqlmodel import Session

    from api.models import ChatMessage


CHANNEL = "qq"


@dataclass
class QQRouteHandle:
    """Bundle of ``(row, target_id, target_type)`` returned to the adapter.

    ``row`` is the live ``BotSessionRoute`` instance — mutate ``next_msg_seq``
    on it and the caller commits inside the same session that loaded it.
    """

    row: BotSessionRoute
    target_id: str
    target_type: str

    @property
    def source_message_id(self) -> str:
        return str(self.row.source_message_id or "")

    @property
    def source_event_id(self) -> str:
        return str(self.row.source_event_id or "")

    @property
    def next_msg_seq(self) -> int:
        return int(self.row.next_msg_seq or 1)


def _decode_target(row: BotSessionRoute) -> tuple[str, str]:
    try:
        target = json.loads(row.target_json or "{}")
    except Exception:
        target = {}
    return (
        str(target.get("target_id", "") or ""),
        str(target.get("target_type", "c2c") or "c2c"),
    )


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
    next_msg_seq: int = 1,
) -> None:
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
    now = time.time()
    if row is None:
        row = BotSessionRoute(
            channel=CHANNEL,
            user_id=int(user_id),
            ai_config_id=int(ai_config_id),
            ai_kind=str(ai_kind or "core"),
            session_id=session_id,
            target_json=target_json,
            source_message_id=str(source_message_id or ""),
            source_event_id=str(source_event_id or ""),
            next_msg_seq=max(1, int(next_msg_seq or 1)),
        )
    else:
        row.target_json = target_json
        row.source_message_id = str(source_message_id or "")
        row.source_event_id = str(source_event_id or "")
        row.next_msg_seq = max(1, int(next_msg_seq or 1))
        row.updated_at = now
    session.add(row)
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
    return QQRouteHandle(row=row, target_id=target_id, target_type=target_type)
