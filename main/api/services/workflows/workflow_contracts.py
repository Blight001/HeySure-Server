"""Workflow device contract snapshots."""
from __future__ import annotations

from typing import Any, Dict, List, Optional, Tuple
from sqlmodel import Session, select
from api.devices.presence import tool_defs_for_agent
from api.models import DevicePresence
from .compiler import WorkflowValidationError, schema_digest

def _contract_device_ids(device_id: Optional[str], device_ids: Optional[List[str]]) -> List[str]:
    values = [str(item).strip() for item in (device_ids or []) if str(item).strip()]
    legacy = str(device_id or "").strip()
    if legacy:
        values.append(legacy)
    return list(dict.fromkeys(values))


def _device_snapshot(session: Session, user_id: int, device_id: str) -> Tuple[DevicePresence, Dict[str, Any]]:
    device = session.exec(select(DevicePresence).where(
        DevicePresence.user_id == user_id,
        DevicePresence.device_id == device_id,
    )).first()
    if not device:
        raise WorkflowValidationError([
            f"contract device is not connected or not owned by the current user: {device_id}; "
            "connect the bound device so toolRef.schemaDigest can be resolved and verified automatically"
        ])
    if not device.online:
        raise WorkflowValidationError([
            f"contract device is offline: {device_id}; bring it online so toolRef.schemaDigest "
            "can be resolved and verified automatically"
        ])
    # Resolve through the public card_service seam so tests and deployments can
    # replace device discovery without mutating this snapshot module.
    from . import card_service
    return device, card_service.tool_defs_for_agent(user_id, device_id)


def _step_device_id(
    step_id: str,
    ref: Dict[str, Any],
    bound_ids: List[str],
    errors: List[str],
) -> str:
    target = str(ref.get("deviceId") or "").strip()
    if not target and len(bound_ids) == 1:
        target = bound_ids[0]
    if not target:
        errors.append(f"step {step_id}: select a contract device for this MCP node")
        return ""
    if bound_ids and target not in bound_ids:
        errors.append(f"step {step_id}: device {target} is not selected as a contract device")
        return ""
    ref["deviceId"] = target
    return target


def _frozen_step_contract(
    *,
    name: str,
    ref: Dict[str, Any],
    target_id: str,
    snapshot: Optional[Tuple[DevicePresence, Dict[str, Any]]],
    errors: List[str],
    step_id: str,
) -> Optional[Dict[str, Any]]:
    supplied = str(ref.get("schemaDigest") or "").strip()
    if snapshot is None:
        if not supplied:
            errors.append(
                f"step {step_id}: schemaDigest cannot be resolved; bind an online contract device "
                "so toolRef.schemaDigest can be filled and verified automatically"
            )
            return None
        return {
            "namespace": "device", "name": name, "deviceId": target_id,
            "schemaDigest": supplied, "inputSchema": ref.get("inputSchema", {}),
            "destructive": False, "provider": str(ref.get("provider") or ""),
            "providers": [], "publishedDeviceId": target_id,
            "publishedDeviceIds": [target_id] if target_id else [],
        }
    device, live_defs = snapshot
    live = live_defs.get(name)
    if not isinstance(live, dict):
        errors.append(f"step {step_id}: tool {name} is not reported by device {target_id}")
        return None
    input_schema = live.get("input_schema") if isinstance(live.get("input_schema"), dict) else {}
    digest = schema_digest(input_schema)
    if supplied and supplied != digest:
        errors.append(f"step {step_id}: supplied schema digest does not match device {target_id}")
        return None
    provider = str(device.device_type or "custom")
    ref["schemaDigest"] = digest
    ref["provider"] = provider
    return {
        "namespace": "device", "name": name, "deviceId": target_id,
        "schemaDigest": digest, "inputSchema": input_schema,
        "destructive": bool(live.get("destructive")), "provider": provider,
        "providers": [provider], "publishedDeviceId": target_id,
        "publishedDeviceIds": [target_id],
    }


def _declared_device_ids(definition: Dict[str, Any]) -> List[str]:
    values: List[str] = []
    for step in definition["steps"].values():
        ref = step.get("toolRef") if step.get("type") == "mcp" else None
        if isinstance(ref, dict):
            values.append(str(ref.get("deviceId") or "").strip())
    return list(dict.fromkeys(item for item in values if item))


def _snapshot_contracts(
    session: Session,
    user_id: int,
    definition: Dict[str, Any],
    *,
    device_id: Optional[str] = None,
    device_ids: Optional[List[str]] = None,
) -> Tuple[Dict[str, Any], List[str]]:
    fallback_ids = _contract_device_ids(device_id, device_ids)
    declared_ids = _declared_device_ids(definition)
    device_steps = [
        step_id for step_id, step in definition["steps"].items()
        if step.get("type") == "mcp"
    ]
    all_nodes_bound = all(
        str(definition["steps"][step_id].get("toolRef", {}).get("deviceId") or "").strip()
        for step_id in device_steps
    )
    bound_ids = declared_ids if all_nodes_bound else list(dict.fromkeys(fallback_ids + declared_ids))
    if device_steps and not bound_ids:
        raise WorkflowValidationError([
            "each device MCP node must declare toolRef.deviceId, or provide one fallback device"
        ])
    snapshots = {item: _device_snapshot(session, user_id, item) for item in bound_ids}
    contracts: Dict[str, Any] = {}
    errors: List[str] = []
    for step_id, step in definition["steps"].items():
        if step.get("type") != "mcp":
            continue
        ref = step["toolRef"]
        name = str(ref["name"]).strip()
        target_id = _step_device_id(step_id, ref, bound_ids, errors)
        if not target_id:
            continue
        contract = _frozen_step_contract(
            name=name,
            ref=ref,
            target_id=target_id,
            snapshot=snapshots.get(target_id),
            errors=errors,
            step_id=step_id,
        )
        if contract is not None:
            contracts[step_id] = contract
    if errors:
        raise WorkflowValidationError(errors)
    return contracts, bound_ids


