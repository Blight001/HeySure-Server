"""Opaque connection/contact directory shared by bot adapters and MCP tools."""

from __future__ import annotations

import hashlib
import json
import time
import uuid
from dataclasses import dataclass
from typing import Any, Dict, Optional

from sqlalchemy import inspect
from sqlmodel import Session, select

from api.models import BotConnection, BotContact, BotSessionRoute, ChatSession
from api.services.bot_credentials import decrypt_credentials, encrypt_credentials


@dataclass(frozen=True)
class ContactTarget:
    connection: BotConnection
    contact: BotContact
    target: Dict[str, Any]


def resolve_connection(
    session: Session,
    *,
    user_id: int,
    ai_config_id: int,
    channel: str,
    connection_ref: str = "",
) -> Optional[BotConnection]:
    stmt = select(BotConnection).where(
        BotConnection.user_id == int(user_id),
        BotConnection.ai_config_id == int(ai_config_id),
        BotConnection.channel == str(channel),
        BotConnection.enabled.is_(True),
        BotConnection.state != "deleted",
    )
    if connection_ref:
        stmt = stmt.where(BotConnection.connection_ref == str(connection_ref))
    return session.exec(stmt.order_by(BotConnection.is_default.desc(), BotConnection.created_at.asc())).first()


def _ref(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex[:20]}"


def _identity_hash(channel: str, identity_key: str) -> str:
    raw = f"{channel.strip().lower()}\0{identity_key.strip()}".encode("utf-8")
    return hashlib.sha256(raw).hexdigest()


def ensure_connection(
    session: Session,
    *,
    user_id: int,
    ai_config_id: int,
    channel: str,
    name: str = "",
    connection_ref: str = "",
    create_new: bool = False,
) -> BotConnection:
    channel = str(channel or "").strip().lower()
    stmt = select(BotConnection).where(
        BotConnection.user_id == int(user_id),
        BotConnection.ai_config_id == int(ai_config_id),
        BotConnection.channel == channel,
    )
    if connection_ref:
        stmt = stmt.where(BotConnection.connection_ref == str(connection_ref))
    row = None if create_new else session.exec(
        stmt.order_by(BotConnection.is_default.desc(), BotConnection.created_at.asc())
    ).first()
    now = time.time()
    if row is None:
        row = BotConnection(
            connection_ref=str(connection_ref or _ref("conn")),
            name=str(name or channel),
            enabled=True,
            user_id=int(user_id),
            ai_config_id=int(ai_config_id),
            channel=channel,
            provider=channel,
            provider_account_id="",
            created_at=now,
            updated_at=now,
        )
    else:
        if not row.connection_ref:
            row.connection_ref = _ref("conn")
        if name and not row.name:
            row.name = str(name)
        row.updated_at = now
    session.add(row)
    session.flush()
    return row


def connection_config(row: BotConnection, defaults: Dict[str, Any]) -> Dict[str, Any]:
    """Decrypt one instance's channel config, falling back to defaults."""
    out = dict(defaults)
    if not row.credentials_encrypted:
        return out
    envelope = decrypt_credentials(row.credentials_encrypted)
    values = envelope.get("bot_config") if isinstance(envelope, dict) else None
    if isinstance(values, dict):
        for key in defaults:
            if key in values:
                out[key] = values[key]
    out["enabled"] = bool(row.enabled)
    return out


def update_connection_config(row: BotConnection, values: Dict[str, Any], defaults: Dict[str, Any]) -> None:
    """Merge whitelisted config without overwriting provider login secrets."""
    envelope = decrypt_credentials(row.credentials_encrypted) if row.credentials_encrypted else {}
    if not isinstance(envelope, dict):
        envelope = {}
    current = connection_config(row, defaults)
    for key in defaults:
        if key in values:
            if ("secret" in key or "token" in key) and values[key] in {"", None}:
                continue
            current[key] = values[key]
    row.enabled = bool(current.get("enabled"))
    envelope["bot_config"] = current
    row.credentials_encrypted = encrypt_credentials(envelope)
    row.updated_at = time.time()


def config_view_for_connection(cfg, row: BotConnection, defaults: Dict[str, Any]):
    """Return a detached AI config whose channel slice comes from one instance."""
    clone = cfg.model_copy(deep=True)
    try:
        payload = json.loads(str(getattr(clone, "bot_configs", "") or "{}"))
    except Exception:
        payload = {}
    payload[row.channel] = connection_config(row, defaults)
    clone.bot_configs = json.dumps(payload, ensure_ascii=False)
    return clone


def attach_route_contact(
    session: Session,
    *,
    route: BotSessionRoute,
    identity_key: str,
    target: Dict[str, Any],
    display_name: str = "",
) -> Optional[BotContact]:
    # Small route-store unit tests intentionally create only the route table.
    # Production schema guards guarantee these tables exist after migration.
    bind = session.connection()
    if not inspect(bind).has_table("botconnection") or not inspect(bind).has_table("botcontact"):
        return None
    connection = session.get(BotConnection, route.connection_id) if route.connection_id else None
    if connection is None:
        connection = ensure_connection(
            session,
            user_id=route.user_id,
            ai_config_id=route.ai_config_id,
            channel=route.channel,
        )
    identity_digest = _identity_hash(route.channel, identity_key)
    contact = session.exec(select(BotContact).where(
        BotContact.connection_id == int(connection.id),
        BotContact.external_id_hash == identity_digest,
    )).first()
    now = time.time()
    encrypted = encrypt_credentials({"target": target})
    if contact is None:
        contact = BotContact(
            connection_id=int(connection.id),
            user_id=route.user_id,
            ai_config_id=route.ai_config_id,
            contact_ref=_ref("recipient"),
            external_id_hash=identity_digest,
            display_name=str(display_name or ""),
            target_encrypted=encrypted,
            last_seen_at=now,
            created_at=now,
            updated_at=now,
        )
    else:
        contact.target_encrypted = encrypted
        contact.last_seen_at = now
        contact.updated_at = now
        if display_name:
            contact.display_name = str(display_name)
    session.add(contact)
    session.flush()
    route.connection_id = int(connection.id)
    route.contact_id = int(contact.id)
    session.add(route)
    chat = session.exec(select(ChatSession).where(
        ChatSession.user_id == route.user_id,
        ChatSession.ai_config_id == route.ai_config_id,
        ChatSession.ai_kind == route.ai_kind,
        ChatSession.session_id == route.session_id,
    )).first()
    if chat is None:
        chat = ChatSession(
            user_id=route.user_id,
            ai_config_id=route.ai_config_id,
            ai_kind=route.ai_kind,
            session_id=route.session_id,
            session_name=f"{route.channel}机器人对话",
            bot_connection_id=int(connection.id),
            bot_contact_id=int(contact.id),
            created_at=now,
            updated_at=now,
        )
    else:
        chat.bot_connection_id = int(connection.id)
        chat.bot_contact_id = int(contact.id)
    session.add(chat)
    session.info["bot_contact_id"] = int(contact.id)
    return contact


def resolve_contact_target(
    session: Session,
    *,
    user_id: int,
    ai_config_id: int,
    connection_ref: str,
    contact_ref: str,
) -> Optional[ContactTarget]:
    stmt = select(BotContact, BotConnection).join(
        BotConnection, BotConnection.id == BotContact.connection_id
    ).where(
        BotContact.user_id == int(user_id),
        BotContact.ai_config_id == int(ai_config_id),
        BotContact.contact_ref == str(contact_ref),
        BotContact.enabled.is_(True),
        BotConnection.enabled.is_(True),
    )
    if connection_ref:
        stmt = stmt.where(BotConnection.connection_ref == str(connection_ref))
    row = session.exec(stmt).first()
    if row is None:
        return None
    contact, connection = row
    envelope = decrypt_credentials(contact.target_encrypted)
    target = envelope.get("target") if isinstance(envelope, dict) else None
    return ContactTarget(connection=connection, contact=contact, target=target if isinstance(target, dict) else {})


def public_connection(row: BotConnection) -> Dict[str, Any]:
    return {
        "connection_ref": row.connection_ref,
        "channel": row.channel,
        "name": row.name or row.channel,
        "enabled": bool(row.enabled),
        "is_default": bool(row.is_default),
        "state": row.state,
        "last_seen_at": row.last_seen_at,
        "created_at": row.created_at,
    }


def public_contact(row: BotContact) -> Dict[str, Any]:
    return {
        "recipient_ref": row.contact_ref,
        "display_name": row.display_name or "未命名联系人",
        "enabled": bool(row.enabled),
        "allow_proactive": bool(row.allow_proactive),
        "last_seen_at": row.last_seen_at,
    }
