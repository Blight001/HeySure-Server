"""Device dynamic MCP tools: CRUD for web-authored, device-type-scoped tools.

An operator manages dynamic MCP tools (``call`` / ``set`` / ``return`` JSON
programs) per device type (``desktop`` / ``browser``) from the web console. The
server validates them the same way the device interpreter does, stores them,
and pushes the enabled set to every online device of that type so a tool change
ships without a new client release.
"""

from typing import Any, Dict, List

from fastapi import APIRouter, Depends, Header, HTTPException, Query
from pydantic import BaseModel, Field
from sqlmodel import Session

from api.database import get_session
from api.device_live import push_device_dynamic_tools
from api.device_presence import online_tool_catalog_for_user
from api.services import device_workspace_tools as dyn
from .auth import get_current_user

router = APIRouter()
PREFIX = "/api/device-tools"


class DynamicToolUpsert(BaseModel):
    device_type: str = Field(..., description="desktop | browser")
    definition: Dict[str, Any] = Field(..., description="{name, description, input_schema, code}")
    enabled: bool = True


class DynamicToolToggle(BaseModel):
    device_type: str
    name: str
    enabled: bool


class DynamicToolStatus(BaseModel):
    device_type: str
    name: str
    status: str = Field(..., description="active | draft | disabled | archived")


class DynamicToolRestore(BaseModel):
    device_type: str
    version_id: int


class PermissionPolicyUpdate(BaseModel):
    device_type: str
    policy: Dict[str, str] = Field(default_factory=dict, description="{permission_tag: allow|confirm|deny}")


def _available_call_targets(user_id: int, device_type: str) -> List[Dict[str, str]]:
    """Tool names the visual editor can offer as ``call`` targets: every tool an
    online device of this type currently advertises (built-ins included), so the
    operator picks from what the device can actually run."""
    targets: Dict[str, str] = {}
    for device in online_tool_catalog_for_user(user_id):
        if str(device.get("device_type") or "") != device_type:
            continue
        for tool in device.get("tools") or []:
            name = str(tool.get("name") or "").strip()
            if name:
                targets.setdefault(name, str(tool.get("description") or "").strip())
    return [{"name": name, "description": targets[name]} for name in sorted(targets)]


@router.get("")
async def list_device_tools(
    device_type: str = Query(...),
    session: Session = Depends(get_session),
    authorization: str = Header(None),
):
    user = get_current_user(authorization, session)
    try:
        dtype = dyn.normalize_device_type(device_type)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    return {
        "deviceType": dtype,
        "tools": dyn.list_tools(user.id, dtype),
        # Tools the operator can target in a ``call`` step (live device catalog).
        "availableTools": _available_call_targets(user.id, dtype),
    }


@router.post("")
async def upsert_device_tool(
    payload: DynamicToolUpsert,
    session: Session = Depends(get_session),
    authorization: str = Header(None),
):
    user = get_current_user(authorization, session)
    try:
        dtype = dyn.normalize_device_type(payload.device_type)
        tool = dyn.upsert_tool(user.id, dtype, payload.definition, enabled=payload.enabled)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    reached = await push_device_dynamic_tools(user.id, dtype)
    return {"tool": tool, "pushedToDevices": reached}


@router.post("/toggle")
async def toggle_device_tool(
    payload: DynamicToolToggle,
    session: Session = Depends(get_session),
    authorization: str = Header(None),
):
    user = get_current_user(authorization, session)
    try:
        dtype = dyn.normalize_device_type(payload.device_type)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    tool = dyn.set_enabled(user.id, dtype, payload.name, payload.enabled)
    if tool is None:
        raise HTTPException(status_code=404, detail="Dynamic MCP tool not found")
    reached = await push_device_dynamic_tools(user.id, dtype)
    return {"tool": tool, "pushedToDevices": reached}


@router.post("/status")
async def set_device_tool_status(
    payload: DynamicToolStatus,
    session: Session = Depends(get_session),
    authorization: str = Header(None),
):
    """Approve (active) or shelve (draft/disabled/archived) a tool. AI-authored
    tools land as draft; this is how an operator promotes them."""
    user = get_current_user(authorization, session)
    try:
        dtype = dyn.normalize_device_type(payload.device_type)
        tool = dyn.set_status(user.id, dtype, payload.name, payload.status)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    if tool is None:
        raise HTTPException(status_code=404, detail="Dynamic MCP tool not found")
    reached = await push_device_dynamic_tools(user.id, dtype)
    return {"tool": tool, "pushedToDevices": reached}


@router.get("/stats")
async def device_tool_stats(
    device_type: str = Query(...),
    session: Session = Depends(get_session),
    authorization: str = Header(None),
):
    user = get_current_user(authorization, session)
    from api.services import mcp_stats

    try:
        dtype = dyn.normalize_device_type(device_type)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    tool_names = [t["name"] for t in dyn.list_tools(user.id, dtype)]
    return {"deviceType": dtype, "stats": mcp_stats.tool_stats(user.id, tool_names)}


@router.get("/failures")
async def device_tool_failures(
    name: str = Query(...),
    session: Session = Depends(get_session),
    authorization: str = Header(None),
):
    user = get_current_user(authorization, session)
    from api.services import mcp_stats

    return {"name": name, "failures": mcp_stats.recent_failures(user.id, name)}


@router.get("/versions")
async def list_device_tool_versions(
    device_type: str = Query(...),
    name: str = Query(...),
    session: Session = Depends(get_session),
    authorization: str = Header(None),
):
    user = get_current_user(authorization, session)
    try:
        dtype = dyn.normalize_device_type(device_type)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    return {"deviceType": dtype, "name": name, "versions": dyn.list_versions(user.id, dtype, name)}


@router.get("/version")
async def get_device_tool_version(
    device_type: str = Query(...),
    version_id: int = Query(...),
    session: Session = Depends(get_session),
    authorization: str = Header(None),
):
    user = get_current_user(authorization, session)
    try:
        dtype = dyn.normalize_device_type(device_type)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    snapshot = dyn.get_version(user.id, dtype, version_id)
    if snapshot is None:
        raise HTTPException(status_code=404, detail="Version not found")
    return {"version": snapshot}


@router.post("/restore")
async def restore_device_tool(
    payload: DynamicToolRestore,
    session: Session = Depends(get_session),
    authorization: str = Header(None),
):
    user = get_current_user(authorization, session)
    try:
        dtype = dyn.normalize_device_type(payload.device_type)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    tool = dyn.restore_version(user.id, dtype, payload.version_id, actor="web")
    if tool is None:
        raise HTTPException(status_code=404, detail="Version not found")
    reached = await push_device_dynamic_tools(user.id, dtype)
    return {"tool": tool, "pushedToDevices": reached}


@router.get("/runtimes")
async def device_runtimes(
    device_type: str = Query("desktop"),
    session: Session = Depends(get_session),
    authorization: str = Header(None),
):
    """Which runtimes (python/powershell/shell) the user's online devices of this
    type can actually execute — drives the 'no online device supports X' hint."""
    user = get_current_user(authorization, session)
    from connector_runtime.dispatch.desktop_device_tools import online_runtimes

    try:
        dtype = dyn.normalize_device_type(device_type)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    return {"deviceType": dtype, "runtimes": online_runtimes(user.id, dtype)}


@router.get("/permission-policy")
async def get_permission_policy(
    device_type: str = Query(...),
    session: Session = Depends(get_session),
    authorization: str = Header(None),
):
    user = get_current_user(authorization, session)
    from api.services import device_permission_policy as policy_svc

    try:
        dtype = dyn.normalize_device_type(device_type)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    return {
        "deviceType": dtype,
        "policy": policy_svc.get_policy(user.id, dtype),
        "knownTags": policy_svc.known_tags(),
    }


@router.post("/permission-policy")
async def set_permission_policy(
    payload: PermissionPolicyUpdate,
    session: Session = Depends(get_session),
    authorization: str = Header(None),
):
    user = get_current_user(authorization, session)
    from api.services import device_permission_policy as policy_svc

    try:
        dtype = dyn.normalize_device_type(payload.device_type)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    policy = policy_svc.set_policy(user.id, dtype, payload.policy)
    # Push so online devices apply the new policy immediately (it rides in the
    # device:tool-config payload).
    reached = await push_device_dynamic_tools(user.id, dtype)
    return {"deviceType": dtype, "policy": policy, "pushedToDevices": reached}


@router.delete("/{name}")
async def delete_device_tool(
    name: str,
    device_type: str = Query(...),
    session: Session = Depends(get_session),
    authorization: str = Header(None),
):
    user = get_current_user(authorization, session)
    try:
        dtype = dyn.normalize_device_type(device_type)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    if not dyn.delete_tool(user.id, dtype, name):
        raise HTTPException(status_code=404, detail="Dynamic MCP tool not found")
    reached = await push_device_dynamic_tools(user.id, dtype)
    return {"ok": True, "pushedToDevices": reached}
