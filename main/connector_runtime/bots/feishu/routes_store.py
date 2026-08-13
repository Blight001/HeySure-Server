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
from fastapi import HTTPException

from api.models import BotConnection, BotSessionRoute
from api.services.bot_directory import attach_route_contact
from ..connection_scope import resolve_inbound_config, scoped_home_session, scoped_identity
from ._config import FEISHU_DEFAULTS, read_feishu_config

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
    connection_ref: str = ""


def _to_view(row: BotSessionRoute, connection_ref: str = "") -> FeishuRouteView:
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
        connection_ref=connection_ref,
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
    connection_ref: str = "",
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
    if connection_ref:
        connection = session.exec(select(BotConnection).where(
            BotConnection.connection_ref == connection_ref,
            BotConnection.ai_config_id == int(ai_config_id),
            BotConnection.channel == CHANNEL,
        )).first()
        if connection:
            row.connection_id = connection.id
    session.add(row)
    session.flush()
    attach_route_contact(
        session,
        route=row,
        identity_key=receive_id,
        target={"receive_id": receive_id, "receive_id_type": receive_id_type},
    )
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
        connection_ref="",
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
        connection = session.get(BotConnection, row.connection_id) if row.connection_id else None
        return _to_view(row, connection.connection_ref if connection else "")
    return _route_from_session_id(message)
def scope_feishu_inbound(session, cfg, connection_ref: str, read_config=read_feishu_config):
    cfg, connection_ref = resolve_inbound_config(
        session, cfg, channel="feishu", connection_ref=connection_ref, defaults=FEISHU_DEFAULTS,
    )
    if not read_config(cfg).get("enabled"):
        raise HTTPException(status_code=400, detail="Feishu bot is disabled for this AI")
    return cfg, connection_ref


def feishu_scope_keys(connection_ref: str, config_id: int, receive_id: str, session_key: str):
    return scoped_identity(connection_ref, receive_id), scoped_home_session(
        "feishu", config_id, connection_ref, session_key,
    )
