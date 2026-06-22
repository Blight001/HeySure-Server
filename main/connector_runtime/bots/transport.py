"""Shared transport plumbing for bot service modules.

Feishu and QQ services each re-implemented the same three concerns: parsing an
open-platform JSON response, caching the access token with an expiry skew, and
the "load the AI config + guard channel/enabled/credentials" preamble. Those
are consolidated here so each channel keeps only its wire-specific bits:

* :func:`parse_json_response` — content-type-aware JSON decode (DRY).
* :class:`TokenCache`        — per-config token cache with a refresh skew
  (single-responsibility; the channel supplies only the fetch closure).
* :func:`load_active_config` — the config-load + channel/enabled guard template
  (template method; the channel supplies its own credential validator).
"""

from __future__ import annotations

import threading
import time
from typing import Any, Callable, Dict, Optional

import requests
from fastapi import HTTPException
from sqlmodel import Session, select

from api.database import engine
from api.models import AssistantAIConfig


def parse_json_response(res: requests.Response) -> Dict[str, Any]:
    """Decode a response body as JSON only when it advertises JSON."""
    if res.headers.get("content-type", "").lower().startswith("application/json"):
        return res.json()
    return {}


class TokenCache:
    """Thread-safe access-token cache keyed by AI config id.

    ``refresh_skew`` keeps a margin so a token never expires mid-request;
    ``min_ttl`` floors the stored lifetime. The channel passes a ``fetch``
    closure returning ``(token, ttl_seconds)`` — this class owns only the
    caching policy.
    """

    def __init__(self, *, refresh_skew: float = 120.0, min_ttl: int = 60) -> None:
        self._cache: Dict[int, Dict[str, Any]] = {}
        self._lock = threading.Lock()
        self._refresh_skew = refresh_skew
        self._min_ttl = min_ttl

    def get_or_fetch(self, config_id: int, fetch: Callable[[], "tuple[str, int]"]) -> str:
        cid = int(config_id or 0)
        now = time.time()
        with self._lock:
            entry = self._cache.get(cid)
        if entry and entry.get("token") and float(entry.get("expires_at") or 0) > now + self._refresh_skew:
            return str(entry["token"])
        token, ttl = fetch()
        token = str(token or "").strip()
        with self._lock:
            self._cache[cid] = {"token": token, "expires_at": time.time() + max(self._min_ttl, int(ttl))}
        return token


def load_active_config(
    user_id: int,
    ai_config_id: Optional[int],
    *,
    channel: str,
    tool_name: str,
    channel_label: str,
    read_config: Callable[[AssistantAIConfig], Dict[str, Any]],
    validate_credentials: Callable[[Dict[str, Any]], None],
) -> AssistantAIConfig:
    """Load the AI config and assert this channel is active, enabled, configured.

    ``validate_credentials`` is the channel's own credential check; it receives
    the channel config slice and raises an ``HTTPException`` with a
    channel-specific message when something required is missing.
    """
    if not ai_config_id:
        raise HTTPException(status_code=400, detail=f"{tool_name} tool requires ai_config_id")
    with Session(engine) as session:
        cfg = session.exec(
            select(AssistantAIConfig).where(
                AssistantAIConfig.id == ai_config_id,
                AssistantAIConfig.user_id == user_id,
            )
        ).first()
    if not cfg:
        raise HTTPException(status_code=404, detail="AI config not found")
    if str(cfg.bot_channel or "feishu").strip().lower() != channel:
        raise HTTPException(status_code=400, detail=f"{channel_label} bot is not the active channel for this AI")
    bot_cfg = read_config(cfg)
    if not bot_cfg.get("enabled"):
        raise HTTPException(status_code=400, detail=f"{channel_label} bot is not enabled for this AI")
    validate_credentials(bot_cfg)
    return cfg
