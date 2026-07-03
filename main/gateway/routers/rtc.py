"""``/api/rtc`` routes: WebRTC ICE (STUN/TURN) config for remote control.

Clients receive ``ice_servers`` once in the login response, but sessions
outlive a single login (long-lived agents, re-connecting viewers), so this
endpoint lets any authenticated caller re-fetch the current server-configured
ICE servers without signing in again. The values come from the same admin
config / env source as login (see ``api.services.access.ice_settings``).
"""

from __future__ import annotations

from typing import Optional

from fastapi import APIRouter, Depends, Header, HTTPException
from sqlmodel import Session

from api.database import get_session
from api.services.access import ice_settings
from gateway.routers.auth import get_current_user


router = APIRouter()
PREFIX = "/api/rtc"


@router.get("/ice-servers")
def ice_servers(
    authorization: Optional[str] = Header(None),
    session: Session = Depends(get_session),
) -> dict:
    if not authorization:
        raise HTTPException(status_code=401, detail="Missing authentication token")
    get_current_user(authorization, session)
    return {"ice_servers": ice_settings.build_ice_servers(session)}
