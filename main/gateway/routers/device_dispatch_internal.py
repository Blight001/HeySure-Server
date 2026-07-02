"""Internal agent-dispatch endpoints served by api-gateway.

Desktop / browser agents register their Socket.IO connection on the
api-gateway (their single public URL), so the live ``agents`` registry and
the socket able to emit ``task:dispatch`` both live in this process. ai-runtime
therefore fires endpoint-tool dispatches here over HTTP (via
``HEYSURE_API_GATEWAY_URL``) and polls for the result.

These mirror the connector-runtime variants (kept there for the monolith /
legacy path); both read the shared ``AgentDispatchTask`` table so a gateway
restart between the POST and the poll doesn't strand the request.

Gated by ``HEYSURE_INTERNAL_TOKEN`` — only other server processes reach it.
"""

from typing import Any, Dict, Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from api.runtime.internal_http import require_internal_token

router = APIRouter(dependencies=[Depends(require_internal_token)])
PREFIX = "/internal/agent"


class DeviceDispatchRequest(BaseModel):
    user_id: int
    ai_config_id: Optional[int] = None
    tool: str
    arguments: Dict[str, Any] = Field(default_factory=dict)
    timeout_seconds: int = 120


@router.post("/dispatch")
async def device_dispatch(req: DeviceDispatchRequest) -> Dict[str, Any]:
    # Non-blocking: emit task:dispatch to the agent + persist a pending row.
    # The caller polls /dispatch/result/{task_id} so a gateway restart between
    # the POST and the poll doesn't strand the request.
    from connector_runtime.dispatch.device_dispatch import dispatch_endpoint_tool
    try:
        task_id = await dispatch_endpoint_tool(
            user_id=req.user_id,
            ai_config_id=req.ai_config_id,
            tool=req.tool,
            args=req.arguments,
        )
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"dispatch failed: {exc}")
    if not task_id:
        raise HTTPException(status_code=503, detail="no agent connected for this tool")
    from sqlmodel import Session, select
    from api.database import engine
    from api.models import AgentDispatchTask
    with Session(engine) as session:
        row = session.exec(
            select(AgentDispatchTask).where(AgentDispatchTask.task_id == task_id)
        ).first()
    return {"ok": True, "task_id": task_id, "status": row.status if row else "pending"}


class DeviceDispatchExpireRequest(BaseModel):
    reason: str = "result wait timed out"


@router.post("/dispatch/expire/{task_id}")
async def device_dispatch_expire(
    task_id: str, req: Optional[DeviceDispatchExpireRequest] = None
) -> Dict[str, Any]:
    # Called by pollers (ai-runtime) that gave up waiting on a dispatch: mark
    # the row timed out and resume the device's queue so the stuck pending row
    # doesn't wedge every later dispatch until the orphan sweep.
    from connector_runtime.dispatch.device_dispatch import expire_dispatch
    expired = await expire_dispatch(
        task_id, reason=(req.reason if req else "result wait timed out")
    )
    return {"ok": True, "expired": expired}


@router.get("/dispatch/result/{task_id}")
def device_dispatch_result(task_id: str) -> Dict[str, Any]:
    # DB-backed lookup so a gateway restart doesn't lose state.
    from sqlmodel import Session, select
    from api.database import engine
    from api.models import AgentDispatchTask
    with Session(engine) as session:
        row = session.exec(
            select(AgentDispatchTask).where(AgentDispatchTask.task_id == task_id)
        ).first()
    if not row:
        raise HTTPException(status_code=404, detail="task not found")
    payload: Dict[str, Any] = {
        "task_id": row.task_id,
        "status": row.status,
        "success": row.success,
        "summary": row.summary,
        "error": row.error,
        "result": None,
        "device_id": row.device_id,
        "tool": row.tool,
    }
    if row.result_json:
        import json as _json
        try:
            payload["result"] = _json.loads(row.result_json)
        except Exception:
            payload["result"] = row.result_json
    return payload
