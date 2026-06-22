"""Feishu-specific reads/writes against the unified ``BotSessionRoute`` table.

``register_feishu_session_route`` upserts a row keyed by
``(channel='feishu', user, ai_config, ai_kind, session_id)`` and stores
the Feishu addressing payload (``receive_id`` + ``receive_id_type``) in
``target_json``. ``load_feishu_route`` reverses that and returns a small
typed view object so the notify orchestrator can keep using
``route.receive_id`` / ``route.receive_id_type`` without parsing JSON
itself.
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


CHANNEL = "feishu"


@dataclass
class FeishuRouteView:
    """Lightweight read-only view of a Feishu route row.

    The notify orchestrator + adapter consume ``receive_id`` /
    ``receive_id_type``; we materialize them once here so callers don't
    have to deal with the JSON envelope.
    """

    user_id: int
    ai_config_id: int
    ai_kind: str
    session_id: str
    receive_id: str
    receive_id_type: str


def _to_view(row: BotSessionRoute) -> FeishuRouteView:
    try:
        target = json.loads(row.target_json or "{}")
    except Exception:
        target = {}
    return FeishuRouteView(
        user_id=int(row.user_id),
        ai_config_id=int(row.ai_config_id),
        ai_kind=str(row.ai_kind or "core"),
        session_id=str(row.session_id or ""),
        receive_id=str(target.get("receive_id", "") or ""),
        receive_id_type=str(target.get("receive_id_type", "chat_id") or "chat_id"),
    )


def register_feishu_session_route(
    session: "Session",
    *,
    user_id: int,
    ai_config_id: int,
    ai_kind: str,
    session_id: str,
    receive_id: str,
    receive_id_type: str,
) -> None:
    session_id = str(session_id or "").strip()
    receive_id = str(receive_id or "").strip()
    receive_id_type = str(receive_id_type or "chat_id").strip() or "chat_id"
    if not session_id or not receive_id:
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
        {"receive_id": receive_id, "receive_id_type": receive_id_type},
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
        )
    else:
        row.target_json = target_json
        row.updated_at = now
    session.add(row)
    session.commit()


def _route_from_session_id(message: "ChatMessage") -> Optional[FeishuRouteView]:
    """Synthesize a route from a legacy ``feishu_<cfg>_<receive_id>`` session id.

    Older messages predate the route table — the session id itself encoded
    the receive_id. We keep the parser so those threads still deliver.
    """
    session_id = str(message.session_id or "")
    ai_config_id = int(message.ai_config_id or 0)
    prefix = f"feishu_{ai_config_id}_"
    if not session_id.startswith(prefix):
        return None
    receive_id = session_id[len(prefix):].strip()
    if not receive_id:
        return None
    return FeishuRouteView(
        user_id=int(message.user_id),
        ai_config_id=ai_config_id,
        ai_kind=str(message.ai_kind or "core"),
        session_id=session_id,
        receive_id=receive_id,
        receive_id_type="chat_id",
    )


def load_feishu_route(
    session: "Session", message: "ChatMessage"
) -> Optional[FeishuRouteView]:
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
    if row is not None:
        return _to_view(row)
    return _route_from_session_id(message)
