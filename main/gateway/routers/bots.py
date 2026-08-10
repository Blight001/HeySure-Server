"""Cross-bot diagnostic + introspection endpoints.

Lives in ``api/routers/`` (not under each bot's package) because the
URLs are bot-agnostic: ``/api/bots/<channel>/diagnose/<config_id>``. Per-bot
event-receive routes still live in each bot's ``router.py``.

Adding a new bot does NOT require touching this file — the channel is
resolved through the registry.
"""

import logging
from typing import Any, Dict, Optional, Tuple

from fastapi import APIRouter, Depends, Header, HTTPException
from sqlmodel import Session

from connector_runtime.bots import all_channels, get as get_bot
from api.core.settings import settings
from api.database import get_session
from api.runtime.internal_http import InternalClient
from api.services.access.access_guards import get_ai_config_or_404
from .auth import get_current_user


logger = logging.getLogger(__name__)

router = APIRouter()
PREFIX = "/api/bots"
_BOT_STATE_FIELDS = ("status", "mode", "label", "message")


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
