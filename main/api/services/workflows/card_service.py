"""CRUD, validation and immutable save-time versioning for workflow cards."""

from __future__ import annotations

import json
import time
import uuid
from typing import Any, Dict, List, Optional, Tuple

from sqlmodel import Session, select

from api.devices.presence import tool_defs_for_agent
from api.models import AssistantAIConfig, DevicePresence, WorkflowCard, WorkflowCardVersion

from .compiler import WorkflowValidationError, compile_definition, definition_digest, schema_digest




def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), default=str)


def _load(raw: Optional[str], fallback: Any) -> Any:
    try:
        return json.loads(raw or "")
    except Exception:
        return fallback


def card_payload(row: WorkflowCard) -> Dict[str, Any]:
    definition = _load(row.draft_definition_json, {})
    return {
        "id": row.id,
        "name": row.name,
        "description": row.description,
        "status": row.status,
        "risk_level": row.risk_level,
        "tags": _load(row.tags_json, []),
        "access_scope": row.access_scope,
        "allowed_ai_config_ids": _load(row.allowed_ai_config_ids_json, []),
        "definition": definition,
        "default_device_id": str(definition.get("defaultDeviceId") or ""),
        "latest_version_id": row.latest_version_id,
        "created_at": row.created_at,
        "updated_at": row.updated_at,
    }


def version_payload(row: WorkflowCardVersion, *, include_definition: bool = False) -> Dict[str, Any]:
    definition = _load(row.definition_json, {})
    payload = {
        "id": row.id,
        "card_id": row.card_id,
        "version_number": row.version_number,
        "schema_version": row.schema_version,
        "definition_digest": row.definition_digest,
        "tool_contracts": _load(row.tool_contracts_json, {}),
        "contract_device_ids": _load(row.contract_device_ids_json, []),
        "default_device_id": str(definition.get("defaultDeviceId") or ""),
        "published_by": row.published_by,
        "published_at": row.published_at,
    }
    if include_definition:
        payload["definition"] = definition
    return payload


def create_card(session: Session, user_id: int, body) -> WorkflowCard:
    now = time.time()
    definition = dict(body.definition or {})
    definition.setdefault("schemaVersion", 1)
    definition.setdefault("name", body.name.strip())
    requested_default = str(
        getattr(body, "default_device_id", None) or getattr(body, "device_id", None) or ""
    ).strip()
    selected_devices = _contract_device_ids(getattr(body, "device_id", None), getattr(body, "device_ids", None))
    if not requested_default and selected_devices:
        requested_default = selected_devices[0]
    if requested_default:
        definition["defaultDeviceId"] = requested_default
    access_scope, allowed_ids = _card_access(session, user_id, body.access_scope, body.allowed_ai_config_ids)
    row = WorkflowCard(
        id=f"wcard_{uuid.uuid4().hex}",
        user_id=user_id,
        created_by=user_id,
        name=body.name.strip(),
        description=body.description.strip(),
        status="active",
        risk_level=body.risk_level,
        tags_json=_json(sorted({tag.strip() for tag in body.tags if tag.strip()})),
        access_scope=access_scope,
        allowed_ai_config_ids_json=_json(allowed_ids),
        draft_definition_json=_json(definition),
        created_at=now,
        updated_at=now,
    )
    session.add(row)
    _save_version(
        session,
        row,
        user_id,
        device_id=getattr(body, "device_id", None),
        device_ids=getattr(body, "device_ids", None),
        default_device_id=requested_default,
    )
    session.refresh(row)
    return row


def owned_card(session: Session, user_id: int, card_id: str) -> Optional[WorkflowCard]:
    return session.exec(
        select(WorkflowCard).where(
            WorkflowCard.id == card_id,
            WorkflowCard.user_id == user_id,
            WorkflowCard.deleted_at.is_(None),
            WorkflowCard.status != "archived",
        )
    ).first()


def delete_card(session: Session, row: WorkflowCard) -> None:
    """Hide a card without breaking immutable versions, runs or audit rows."""
    now = time.time()
    row.deleted_at = now
    row.updated_at = now
    session.add(row)
    session.commit()


def update_card(
    session: Session,
    row: WorkflowCard,
    body,
    *,
    user_id: int,
) -> WorkflowCard:
    payload = body.model_dump(exclude_unset=True)
    for key in ("name", "description", "risk_level"):
        if key in payload and payload[key] is not None:
            setattr(row, key, str(payload[key]).strip())
    if "tags" in payload and payload["tags"] is not None:
        row.tags_json = _json(sorted({str(tag).strip() for tag in payload["tags"] if str(tag).strip()}))
    if "access_scope" in payload or "allowed_ai_config_ids" in payload:
        scope, allowed_ids = _card_access(
            session,
            row.user_id,
            payload.get("access_scope", row.access_scope),
            payload.get("allowed_ai_config_ids", _load(row.allowed_ai_config_ids_json, [])),
        )
        row.access_scope = scope
        row.allowed_ai_config_ids_json = _json(allowed_ids)
    if "definition" in payload and payload["definition"] is not None:
        definition = dict(payload["definition"])
        definition.setdefault("schemaVersion", 1)
        row.draft_definition_json = _json(definition)
    selected_ids, requested_default = _updated_device_selection(session, row, payload)
    row.status = "active"
    row.updated_at = time.time()
    session.add(row)
    _save_version(
        session,
        row,
        user_id,
        device_id=payload.get("device_id"),
        device_ids=selected_ids,
        default_device_id=requested_default,
    )
    session.refresh(row)
    return row


def _updated_device_selection(
    session: Session, row: WorkflowCard, payload: Dict[str, Any]
) -> tuple[List[str], str]:
    latest = session.get(WorkflowCardVersion, row.latest_version_id) if row.latest_version_id else None
    inherited_ids = _load(latest.contract_device_ids_json, []) if latest else []
    selected_ids = payload.get("device_ids")
    selected_ids = inherited_ids if selected_ids is None else selected_ids
    current_definition = _load(row.draft_definition_json, {})
    requested_default = str(
        payload.get("default_device_id") or payload.get("device_id")
        or current_definition.get("defaultDeviceId") or (selected_ids[0] if selected_ids else "")
    ).strip()
    if requested_default:
        current_definition["defaultDeviceId"] = requested_default
        row.draft_definition_json = _json(current_definition)
    return selected_ids, requested_default


def _card_access(session: Session, user_id: int, scope: object, allowed_ids: object) -> Tuple[str, List[int]]:
    normalized_scope = str(scope or "all").strip().lower()
    if normalized_scope not in WorkflowCard.ACCESS_SCOPES:
        raise WorkflowValidationError(["card access scope must be all, owner, or selected"])
    requested = list(dict.fromkeys(
        int(item) for item in (allowed_ids if isinstance(allowed_ids, list) else [])
        if str(item).strip().isdigit() and int(item) > 0
    ))
    if requested:
        existing = session.exec(select(AssistantAIConfig.id).where(
            AssistantAIConfig.user_id == user_id,
            AssistantAIConfig.id.in_(requested),
        )).all()
        if {int(item) for item in existing} != set(requested):
            raise WorkflowValidationError(["one or more allowed AI members do not exist"])
    return normalized_scope, requested if normalized_scope == "selected" else []


def validate_card(row: WorkflowCard, session: Optional[Session] = None) -> Dict[str, Any]:
    compiled = compile_definition(_load(row.draft_definition_json, {}))
    return {"valid": True, "digest": compiled["digest"], "warnings": compiled["warnings"]}


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
    return device, tool_defs_for_agent(user_id, device_id)


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


def _snapshot_contracts(
    session: Session,
    user_id: int,
    definition: Dict[str, Any],
    *,
    device_id: Optional[str] = None,
    device_ids: Optional[List[str]] = None,
) -> Tuple[Dict[str, Any], List[str]]:
    fallback_ids = _contract_device_ids(device_id, device_ids)
    declared_ids = [
        str(step.get("toolRef", {}).get("deviceId") or "").strip()
        for step in definition["steps"].values()
        if step.get("type") == "mcp" and isinstance(step.get("toolRef"), dict)
    ]
    declared_ids = list(dict.fromkeys(item for item in declared_ids if item))
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


def _save_version(
    session: Session,
    row: WorkflowCard,
    user_id: int,
    *,
    device_id: Optional[str] = None,
    device_ids: Optional[List[str]] = None,
    default_device_id: Optional[str] = None,
) -> WorkflowCardVersion:
    compiled = compile_definition(_load(row.draft_definition_json, {}))
    definition = compiled["definition"]
    row.draft_definition_json = _json(definition)
    contracts, bound_ids = _snapshot_contracts(
        session, user_id, definition, device_id=device_id, device_ids=device_ids,
    )
    selected_default = str(default_device_id or definition.get("defaultDeviceId") or "").strip()
    if not selected_default and bound_ids:
        selected_default = bound_ids[0]
    if selected_default and selected_default not in bound_ids:
        raise WorkflowValidationError(["default device must be one of the selected contract device IDs"])
    if selected_default:
        definition["defaultDeviceId"] = selected_default
    definition["contractDeviceIds"] = bound_ids
    row.draft_definition_json = _json(definition)
    latest = session.exec(
        select(WorkflowCardVersion)
        .where(WorkflowCardVersion.card_id == row.id)
        .order_by(WorkflowCardVersion.version_number.desc())
    ).first()
    version = WorkflowCardVersion(
        id=f"wver_{uuid.uuid4().hex}",
        card_id=row.id,
        version_number=(latest.version_number + 1) if latest else 1,
        schema_version=1,
        definition_json=_json(definition),
        definition_digest=definition_digest(definition),
        tool_contracts_json=_json(contracts),
        contract_device_ids_json=_json(bound_ids),
        published_by=user_id,
    )
    session.add(version)
    session.flush()
    row.latest_version_id = version.id
    row.status = "active"
    row.updated_at = time.time()
    session.add(row)
    session.commit()
    session.refresh(version)
    return version
