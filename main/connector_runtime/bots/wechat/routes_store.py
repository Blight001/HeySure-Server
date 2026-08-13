"""WeChat addressing stored in the unified bot session route table."""

from __future__ import annotations

import json
import time
from dataclasses import dataclass
from typing import TYPE_CHECKING, Optional

from sqlmodel import select

from api.models import BotConnection, BotSessionRoute
from api.services.bot_credentials import decrypt_credentials, encrypt_credentials
from api.services.bot_directory import attach_route_contact

if TYPE_CHECKING:
    from sqlmodel import Session
    from api.models import ChatMessage


CHANNEL = "wechat"


@dataclass(frozen=True)
class WeChatRoute:
    to_user_id: str
    context_token: str
    session_id: str
    connection_ref: str = ""


def _decode(row: BotSessionRoute, connection_ref: str = "") -> WeChatRoute:
    try:
        target = json.loads(row.target_json or "{}")
    except Exception:
        target = {}
    try:
        secret = decrypt_credentials(str(target.get("context_encrypted") or ""))
    except ValueError:
        secret = {}
    return WeChatRoute(
        to_user_id=str(target.get("to_user_id") or ""),
        context_token=str(secret.get("context_token") or ""),
        session_id=str(row.session_id or ""),
        connection_ref=connection_ref,
    )


def register_wechat_route(
    session: "Session",
    *,
    user_id: int,
    ai_config_id: int,
    ai_kind: str,
    session_id: str,
    to_user_id: str,
    context_token: str,
    connection_ref: str = "",
) -> None:
    if not session_id or not to_user_id or not context_token:
        return
    row = session.exec(select(BotSessionRoute).where(
        BotSessionRoute.channel == CHANNEL,
        BotSessionRoute.user_id == user_id,
        BotSessionRoute.ai_config_id == ai_config_id,
        BotSessionRoute.ai_kind == ai_kind,
        BotSessionRoute.session_id == session_id,
    )).first()
    target_json = json.dumps({
        "to_user_id": to_user_id,
        "context_encrypted": encrypt_credentials({"context_token": context_token}),
    }, ensure_ascii=False)
    if row is None:
        row = BotSessionRoute(
            channel=CHANNEL,
            user_id=user_id,
            ai_config_id=ai_config_id,
            ai_kind=ai_kind,
            session_id=session_id,
            target_json=target_json,
        )
    else:
        row.target_json = target_json
        row.updated_at = time.time()
    if connection_ref:
        connection = session.exec(select(BotConnection).where(
            BotConnection.connection_ref == connection_ref,
            BotConnection.ai_config_id == ai_config_id,
            BotConnection.channel == CHANNEL,
        )).first()
        if connection:
            row.connection_id = connection.id
    session.add(row)
    session.flush()
    attach_route_contact(
        session,
        route=row,
        identity_key=to_user_id,
        target={"to_user_id": to_user_id, "context_token": context_token},
    )
    session.commit()


def load_wechat_route(session: "Session", message: "ChatMessage") -> Optional[WeChatRoute]:
    if not message.ai_config_id:
        return None
    row = session.exec(select(BotSessionRoute).where(
        BotSessionRoute.channel == CHANNEL,
        BotSessionRoute.user_id == int(message.user_id),
        BotSessionRoute.ai_config_id == int(message.ai_config_id),
        BotSessionRoute.ai_kind == str(message.ai_kind or "core"),
        BotSessionRoute.session_id == str(message.session_id or ""),
    )).first()
    connection = session.get(BotConnection, row.connection_id) if row and row.connection_id else None
    return _decode(row, connection.connection_ref if connection else "") if row else None


def latest_wechat_route(session: "Session", *, user_id: int, ai_config_id: int, connection_ref: str = "") -> Optional[WeChatRoute]:
    stmt = select(BotSessionRoute).where(
        BotSessionRoute.channel == CHANNEL,
        BotSessionRoute.user_id == user_id,
        BotSessionRoute.ai_config_id == ai_config_id,
    )
    connection = None
    if connection_ref:
        connection = session.exec(select(BotConnection).where(
            BotConnection.connection_ref == connection_ref,
            BotConnection.ai_config_id == ai_config_id,
        )).first()
        if connection:
            stmt = stmt.where(BotSessionRoute.connection_id == connection.id)
    row = session.exec(stmt.order_by(BotSessionRoute.updated_at.desc())).first()
    if row and connection is None and row.connection_id:
        connection = session.get(BotConnection, row.connection_id)
    return _decode(row, connection.connection_ref if connection else "") if row else None
