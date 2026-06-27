"""Cross-bot diagnostic + introspection endpoints.

Lives in ``api/routers/`` (not under each bot's package) because the
URLs are bot-agnostic: ``/api/bots/<channel>/diagnose/<config_id>``. Per-bot
event-receive routes still live in each bot's ``router.py``.

Adding a new bot does NOT require touching this file — the channel is
resolved through the registry.
"""

import logging
from typing import Any, Dict

from fastapi import APIRouter, Depends, Header, HTTPException
from sqlmodel import Session

from connector_runtime.bots import all_channels, get as get_bot
from api.database import get_session
from api.services.access.access_guards import get_ai_config_or_404
from .auth import get_current_user


logger = logging.getLogger(__name__)

router = APIRouter()
PREFIX = "/api/bots"


def _resolve_user_cfg(
    config_id: int, session: Session, authorization: str
) -> tuple:
    user = get_current_user(authorization, session)
    cfg = get_ai_config_or_404(session, config_id, user.id)
    return user, cfg


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
        return bot.diagnose(cfg, user_id=int(user.id))
    except Exception as exc:
        logger.exception(f"diagnose failed channel={channel} config_id={config_id}")
        raise HTTPException(status_code=500, detail=f"diagnose failed: {exc}")
