"""Cross-bot diagnostic + introspection endpoints.

Lives in ``api/routers/`` (not under each bot's package) because the
URLs are bot-agnostic: ``/api/bots/<channel>/diagnose/<config_id>``. Per-bot
event-receive routes still live in each bot's ``router.py``.

Adding a new bot does NOT require touching this file — the channel is
resolved through the registry.
"""

import logging
import time
from typing import Any, Dict, Optional, Tuple

from fastapi import APIRouter, Depends, Header, HTTPException
from pydantic import BaseModel, Field
from sqlmodel import Session, select

from connector_runtime.bots import all_channels, get as get_bot
from api.core.settings import settings
from api.database import get_session
from api.runtime.internal_http import InternalClient
from api.services.access.access_guards import get_ai_config_or_404
from api.models import BotConnection, BotContact
from api.services.bot_directory import (
    connection_config,
    ensure_connection,
    public_connection,
    public_contact,
    update_connection_config,
)
from .auth import get_current_user


logger = logging.getLogger(__name__)

router = APIRouter()
PREFIX = "/api/bots"
_BOT_STATE_FIELDS = ("status", "mode", "label", "message")


class BotVerifyCodeRequest(BaseModel):
    value: str


class BotConnectionCreateRequest(BaseModel):
    channel: str
    name: str = ""
    config: Dict[str, Any] = Field(default_factory=dict)


class BotConnectionUpdateRequest(BaseModel):
    name: Optional[str] = None
    enabled: Optional[bool] = None
    is_default: Optional[bool] = None
    config: Optional[Dict[str, Any]] = None


def _connection_view(row: BotConnection) -> Dict[str, Any]:
    out = public_connection(row)
    bot = get_bot(row.channel)
    if bot is not None:
        values = connection_config(row, bot.default_config())
        for key in tuple(values):
            if "secret" in key or "token" in key:
                values[key] = ""
        out["config"] = values
        out["credentials_configured"] = bool(row.credentials_encrypted)
    return out


@router.get("/connections/{config_id}")
def list_bot_connections(
    config_id: int,
    session: Session = Depends(get_session),
    authorization: str = Header(None),
) -> Dict[str, Any]:
    user, _ = _resolve_user_cfg(config_id, session, authorization)
    rows = session.exec(select(BotConnection).where(
        BotConnection.user_id == int(user.id),
        BotConnection.ai_config_id == int(config_id),
        BotConnection.state != "deleted",
    ).order_by(BotConnection.channel)).all()
    statuses: Dict[str, Any] = {}
    connector_url = str(settings.connector_runtime_url or "").strip()
    if connector_url:
        client = InternalClient(connector_url, timeout=2.0)
        try:
            payload = client.get("/internal/bot/statuses")
            statuses = payload.get("connection_statuses") if isinstance(payload, dict) else {}
        except Exception:
            statuses = {}
        finally:
            client.close()
    views = []
    for row in rows:
        view = _connection_view(row)
        if row.connection_ref in statuses:
            view["runtime_status"] = statuses[row.connection_ref]
        views.append(view)
    return {"connections": views}


@router.post("/connections/{config_id}")
def create_bot_connection(
    config_id: int,
    body: BotConnectionCreateRequest,
    session: Session = Depends(get_session),
    authorization: str = Header(None),
) -> Dict[str, Any]:
    user, _ = _resolve_user_cfg(config_id, session, authorization)
    channel = str(body.channel or "").strip().lower()
    bot = get_bot(channel)
    if bot is None:
        raise HTTPException(status_code=400, detail="unknown bot channel")
    existing = session.exec(select(BotConnection).where(
        BotConnection.user_id == int(user.id),
        BotConnection.ai_config_id == int(config_id),
        BotConnection.channel == channel,
        BotConnection.state != "deleted",
    )).first()
    row = ensure_connection(
        session,
        user_id=int(user.id),
        ai_config_id=config_id,
        channel=channel,
        name=body.name or bot.label,
        create_new=True,
    )
    row.is_default = existing is None
    update_connection_config(row, {**body.config, "enabled": True}, bot.default_config())
    row.state = "disconnected" if channel == "wechat" else "configured"
    session.add(row)
    session.commit()
    session.refresh(row)
    return _connection_view(row)


def _owned_connection(session: Session, user_id: int, config_id: int, connection_ref: str) -> BotConnection:
    row = session.exec(select(BotConnection).where(
        BotConnection.user_id == int(user_id),
        BotConnection.ai_config_id == int(config_id),
        BotConnection.connection_ref == str(connection_ref),
        BotConnection.state != "deleted",
    )).first()
    if row is None:
        raise HTTPException(status_code=404, detail="bot connection not found")
    return row


@router.patch("/connections/{config_id}/{connection_ref}")
def update_bot_connection(
    config_id: int,
    connection_ref: str,
    body: BotConnectionUpdateRequest,
    session: Session = Depends(get_session),
    authorization: str = Header(None),
) -> Dict[str, Any]:
    user, _ = _resolve_user_cfg(config_id, session, authorization)
    row = _owned_connection(session, int(user.id), config_id, connection_ref)
    bot = get_bot(row.channel)
    if body.name is not None:
        row.name = str(body.name).strip() or bot.label
    if body.enabled is not None:
        row.enabled = bool(body.enabled)
    if body.config is not None:
        update_connection_config(row, body.config, bot.default_config())
    if body.is_default:
        peers = session.exec(select(BotConnection).where(
            BotConnection.ai_config_id == config_id,
            BotConnection.channel == row.channel,
        )).all()
        for peer in peers:
            peer.is_default = peer.id == row.id
            session.add(peer)
    session.add(row)
    session.commit()
    session.refresh(row)
    return _connection_view(row)


@router.delete("/connections/{config_id}/{connection_ref}")
def delete_bot_connection(
    config_id: int,
    connection_ref: str,
    session: Session = Depends(get_session),
    authorization: str = Header(None),
) -> Dict[str, Any]:
    user, _ = _resolve_user_cfg(config_id, session, authorization)
    row = _owned_connection(session, int(user.id), config_id, connection_ref)
    row.enabled = False
    row.is_default = False
    row.state = "deleted"
    row.updated_at = time.time()
    session.add(row)
    session.commit()
    return {"success": True, "connection_ref": connection_ref}


@router.get("/connections/{config_id}/{connection_ref}/contacts")
def list_bot_contacts(
    config_id: int,
    connection_ref: str,
    session: Session = Depends(get_session),
    authorization: str = Header(None),
) -> Dict[str, Any]:
    user, _ = _resolve_user_cfg(config_id, session, authorization)
    connection = session.exec(select(BotConnection).where(
        BotConnection.user_id == int(user.id),
        BotConnection.ai_config_id == int(config_id),
        BotConnection.connection_ref == str(connection_ref),
    )).first()
    if connection is None:
        raise HTTPException(status_code=404, detail="bot connection not found")
    rows = session.exec(select(BotContact).where(
        BotContact.connection_id == int(connection.id),
    ).order_by(BotContact.last_seen_at.desc())).all()
    return {"contacts": [public_contact(row) for row in rows]}


def _resolve_user_cfg(
    config_id: int, session: Session, authorization: str
) -> tuple:
    user = get_current_user(authorization, session)
    cfg = get_ai_config_or_404(session, config_id, user.id)
    return user, cfg


def _load_connector_bot_status(
    channel: str, config_id: int
) -> Tuple[Optional[Dict[str, str]], Optional[str]]:
    """Read the process-owning Connector runtime's long-connection state."""
    connector_url = str(settings.connector_runtime_url or "").strip()
    if not connector_url:
        return None, None
    client = InternalClient(connector_url, timeout=1.5)
    try:
        payload = client.get("/internal/bot/statuses")
    except Exception as exc:
        logger.warning(
            "connector bot status unavailable channel=%s config_id=%s error_type=%s",
            channel,
            config_id,
            type(exc).__name__,
        )
        return None, "connector_runtime_unavailable"
    finally:
        client.close()

    states = payload.get(f"{channel}_statuses") if isinstance(payload, dict) else None
    state = states.get(str(config_id)) if isinstance(states, dict) else None
    if not isinstance(state, dict):
        return None, "connector_status_missing"
    return {
        field: str(state.get(field) or "")
        for field in _BOT_STATE_FIELDS
    }, None


def _apply_connector_bot_status(
    result: Dict[str, Any], remote_state: Dict[str, str]
) -> None:
    result["bot_status"] = remote_state
    result["status"] = remote_state.get("status") or "failed"
    credential_ok = result.get("success")
    if credential_ok is None:
        credential_ok = result.get("token_ok")
    if credential_ok is None:
        credential_ok = result.get("ok", True)
    result["ok"] = bool(credential_ok and result["status"] == "success")


def _local_login_call(bot, config_id: int, action: str, user_id: int, payload, connection_ref: str):
    method_name = {
        "start": "start_login", "status": "login_status",
        "verify": "submit_login_verify_code", "logout": "logout",
    }[action]
    method = getattr(bot, method_name, None)
    if method is None:
        raise HTTPException(status_code=404, detail="bot channel does not support QR login")
    if action == "start":
        return method(config_id, user_id, connection_ref)
    if action == "verify":
        return method(config_id, str((payload or {}).get("value") or ""), connection_ref)
    return method(config_id, connection_ref)


def _remote_login_call(client, channel: str, config_id: int, action: str, payload, user_id: int, connection_ref: str):
    suffix = f"?connection_ref={connection_ref}" if connection_ref else ""
    if action == "status":
        return client.get(f"/internal/bot/{channel}/login/{config_id}{suffix}")
    if action == "logout":
        return client.post(f"/internal/bot/{channel}/logout/{config_id}{suffix}")
    endpoint_suffix = "/verify-code" if action == "verify" else ""
    body = {**(payload or {"user_id": user_id}), "connection_ref": connection_ref}
    return client.post(f"/internal/bot/{channel}/login/{config_id}{endpoint_suffix}", json=body)


def _connector_login_call(
    channel: str, config_id: int, action: str, *, user_id: int,
    payload: Optional[Dict[str, Any]] = None, connection_ref: str = "",
) -> Dict[str, Any]:
    bot = get_bot(channel)
    if bot is None:
        raise HTTPException(status_code=404, detail="unknown bot channel")
    connector_url = str(settings.connector_runtime_url or "").strip()
    if not connector_url:
        return _local_login_call(bot, config_id, action, user_id, payload, connection_ref)
    client = InternalClient(connector_url, timeout=45.0 if action == "start" else 10.0)
    try:
        return _remote_login_call(client, channel, config_id, action, payload, user_id, connection_ref)
    except Exception as exc:
        logger.warning("connector bot login call failed channel=%s action=%s error_type=%s", channel, action, type(exc).__name__)
        raise HTTPException(status_code=502, detail="Connector Runtime 微信连接服务不可用") from exc
    finally:
        client.close()


@router.get("/channels")
def list_bot_channels() -> Dict[str, Any]:
    """Return the registered bot channels + their human labels."""
    return {
        "channels": [
            {"channel": ch, "label": (get_bot(ch).label if get_bot(ch) else ch)}
            for ch in all_channels()
        ]
    }


@router.get("/{channel}/diagnose/{config_id}")
def diagnose_bot(
    channel: str,
    config_id: int,
    session: Session = Depends(get_session),
    authorization: str = Header(None),
) -> Dict[str, Any]:
    """Run the channel's self-check against ``config_id`` and return the result.

    Every adapter returns at least ``ok: bool``; richer fields are
    channel-specific.
    """
    user, cfg = _resolve_user_cfg(config_id, session, authorization)
    bot = get_bot(channel)
    if bot is None:
        raise HTTPException(
            status_code=404,
            detail=f"unknown bot channel '{channel}'; registered: {sorted(all_channels())}",
        )
    try:
        result = bot.diagnose(cfg, user_id=int(user.id))
        connection_mode = str(result.get("connection_mode") or "").lower()
        expects_remote_state = bool(
            settings.connector_runtime_url
            and connection_mode not in {"", "none", "webhook"}
        )
        if expects_remote_state:
            remote_state, remote_error = _load_connector_bot_status(channel, config_id)
            if remote_state is not None:
                _apply_connector_bot_status(result, remote_state)
            elif remote_error:
                result["bot_status"] = {
                    "status": "unknown",
                    "mode": connection_mode,
                    "label": "状态未知",
                    "message": "无法读取 Connector Runtime 的连接状态",
                }
                result["status"] = "unknown"
                result["ok"] = False
                result["connector_status_error"] = remote_error
        return result
    except Exception as exc:
        logger.exception(f"diagnose failed channel={channel} config_id={config_id}")
        raise HTTPException(status_code=500, detail=f"diagnose failed: {exc}")


@router.post("/{channel}/login/{config_id}")
def start_bot_login(
    channel: str,
    config_id: int,
    session: Session = Depends(get_session),
    authorization: str = Header(None),
    connection_ref: str = "",
) -> Dict[str, Any]:
    user, cfg = _resolve_user_cfg(config_id, session, authorization)
    bot = get_bot(channel)
    if bot is None:
        raise HTTPException(status_code=404, detail="unknown bot channel")
    bot.apply_config_payload(cfg, {"enabled": True})
    session.add(cfg)
    session.commit()
    return _connector_login_call(channel, config_id, "start", user_id=int(user.id), connection_ref=connection_ref)


@router.get("/{channel}/login/{config_id}")
def get_bot_login_status(
    channel: str,
    config_id: int,
    session: Session = Depends(get_session),
    authorization: str = Header(None),
    connection_ref: str = "",
) -> Dict[str, Any]:
    user, _ = _resolve_user_cfg(config_id, session, authorization)
    return _connector_login_call(channel, config_id, "status", user_id=int(user.id), connection_ref=connection_ref)


@router.post("/{channel}/login/{config_id}/verify-code")
def submit_bot_login_verify_code(
    channel: str,
    config_id: int,
    body: BotVerifyCodeRequest,
    session: Session = Depends(get_session),
    authorization: str = Header(None),
    connection_ref: str = "",
) -> Dict[str, Any]:
    user, _ = _resolve_user_cfg(config_id, session, authorization)
    return _connector_login_call(channel, config_id, "verify", user_id=int(user.id), payload={"value": body.value}, connection_ref=connection_ref)


@router.delete("/{channel}/login/{config_id}")
def disconnect_bot_login(
    channel: str,
    config_id: int,
    session: Session = Depends(get_session),
    authorization: str = Header(None),
    connection_ref: str = "",
) -> Dict[str, Any]:
    user, _ = _resolve_user_cfg(config_id, session, authorization)
    return _connector_login_call(channel, config_id, "logout", user_id=int(user.id), connection_ref=connection_ref)
