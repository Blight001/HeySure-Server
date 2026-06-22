"""Internal relay so ai-runtime / mcp-runtime can emit Socket.IO events.

``api.sio._RemoteSio`` forwards every ``sio.emit(...)`` made in a split
worker process to ``POST /internal/socket/emit`` on api-gateway. This
router unpacks the forwarded payload and calls the real Socket.IO server
locally.

The route is gated by ``HEYSURE_INTERNAL_TOKEN``; without the token only
loopback callers are allowed. End users have no way to reach this route
because there is no path through the normal API auth that arrives here.
"""

from typing import Any, Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from api.chat_runtime.run_state import apply_relayed_run_live_state
from api.runtime.internal_http import require_internal_token
from api.sio import sio


router = APIRouter()
PREFIX = "/internal/socket"


class EmitRequest(BaseModel):
    event: str
    data: Optional[Any] = None
    to: Optional[str] = None
    room: Optional[str] = None
    namespace: Optional[str] = None


@router.post("/emit", dependencies=[Depends(require_internal_token)])
async def relay_emit(req: EmitRequest) -> dict:
    if not req.event:
        raise HTTPException(status_code=400, detail="event required")
    if req.event == "chat:run_live":
        apply_relayed_run_live_state(req.data)
    kwargs = {}
    if req.to is not None:
        kwargs["to"] = req.to
    if req.room is not None:
        kwargs["room"] = req.room
    if req.namespace is not None:
        kwargs["namespace"] = req.namespace
    try:
        await sio.emit(req.event, req.data, **kwargs)
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"emit failed: {exc}")
    return {"ok": True}
