"""Server-scoped WebRTC ICE config: STUN + TURN for remote control.

Values live in the ``SystemSetting`` key/value table so the admin console can
change them at runtime; when a DB value is empty they fall back to the
``HEYSURE_STUN_URL`` / ``HEYSURE_TURN_URL`` / ``HEYSURE_TURN_USERNAME`` /
``HEYSURE_TURN_PASSWORD`` env settings, so headless deploys can be configured
via env alone.

:func:`build_ice_servers` returns the ``RTCIceServer[]`` shape every
remote-control client expects (web console, game viewer, desktop / browser /
mobile agents). It is delivered once at login (see ``auth.py``) and re-fetched
via ``GET /api/rtc/ice-servers``, so the STUN/TURN config lives in exactly one
place instead of being hardcoded per client.
"""

from __future__ import annotations

from sqlmodel import Session

from api.core.settings import settings
from api.services.access.auth_settings import get_setting, set_setting

STUN_URL_KEY = "rtc.stun_url"
TURN_URL_KEY = "rtc.turn_url"
TURN_USERNAME_KEY = "rtc.turn_username"
TURN_PASSWORD_KEY = "rtc.turn_password"


def get_ice_config(session: Session) -> dict:
    """Effective ICE config: DB value first, env fallback.

    ``stun_url`` falls back to the built-in Google STUN default so remote
    control keeps working out of the box; the TURN fields default to empty
    (no relay) until an operator configures one.
    """
    return {
        "stun_url": get_setting(session, STUN_URL_KEY, settings.stun_url),
        "turn_url": get_setting(session, TURN_URL_KEY, settings.turn_url),
        "turn_username": get_setting(session, TURN_USERNAME_KEY, settings.turn_username),
        "turn_password": get_setting(session, TURN_PASSWORD_KEY, settings.turn_password),
    }


def build_ice_servers(session: Session) -> list[dict]:
    """Assemble the ``RTCIceServer[]`` list delivered to every peer.

    STUN comes first (cheap, direct); TURN is appended only when a URL is set
    and carries its long-term credential. The list is safe to hand to a browser
    ``RTCPeerConnection`` or a native WebRTC stack as-is.
    """
    cfg = get_ice_config(session)
    servers: list[dict] = []
    stun = str(cfg.get("stun_url") or "").strip()
    if stun:
        servers.append({"urls": stun})
    turn = str(cfg.get("turn_url") or "").strip()
    if turn:
        entry: dict = {"urls": turn}
        username = str(cfg.get("turn_username") or "").strip()
        password = str(cfg.get("turn_password") or "").strip()
        if username:
            entry["username"] = username
        if password:
            entry["credential"] = password
        servers.append(entry)
    return servers


def turn_configured(session: Session) -> bool:
    return bool(str(get_ice_config(session).get("turn_url") or "").strip())


def save_ice_config(
    session: Session,
    *,
    stun_url: str,
    turn_url: str,
    turn_username: str,
    turn_password: str | None,
) -> None:
    """Persist ICE settings. ``turn_password=None`` keeps the stored password."""
    set_setting(session, STUN_URL_KEY, (stun_url or "").strip())
    set_setting(session, TURN_URL_KEY, (turn_url or "").strip())
    set_setting(session, TURN_USERNAME_KEY, (turn_username or "").strip())
    if turn_password is not None:
        set_setting(session, TURN_PASSWORD_KEY, turn_password)
