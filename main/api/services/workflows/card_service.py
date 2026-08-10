"""CRUD, validation and immutable publishing for workflow cards."""

from __future__ import annotations

import json
import time
import uuid
from typing import Any, Dict, List, Optional, Tuple

from sqlmodel import Session, select

from api.devices.presence import tool_defs_for_agent
from api.models import DevicePresence, WorkflowCard, WorkflowCardVersion

from .compiler import WorkflowValidationError, compile_definition, definition_digest, schema_digest
from .interaction_steps import is_ai_intervention_step




def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), default=str)


def _load(raw: Optional[str], fallback: Any) -> Any:
    try:
        return json.loads(raw or "")
    except Exception:
        return fallback


def card_payload(row: WorkflowCard) -> Dict[str, Any]:
    return {
        "id": row.id,
        "name": row.name,
        "description": row.description,
        "status": row.status,
        "risk_level": row.risk_level,
        "tags": _load(row.tags_json, []),
        "definition": _load(row.draft_definition_json, {}),
        "latest_version_id": row.latest_version_id,
        "created_at": row.created_at,
        "updated_at": row.updated_at,
    }


def version_payload(row: WorkflowCardVersion, *, include_definition: bool = False) -> Dict[str, Any]:
    payload = {
        "id": row.id,
        "card_id": row.card_id,
        "version_number": row.version_number,
        "schema_version": row.schema_version,
        "definition_digest": row.definition_digest,
        "tool_contracts": _load(row.tool_contracts_json, {}),
        "contract_device_ids": _load(row.contract_device_ids_json, []),
        "published_by": row.published_by,
        "published_at": row.published_at,
    }
    if include_definition:
        payload["definition"] = _load(row.definition_json, {})
    return payload


def create_card(session: Session, user_id: int, body) -> WorkflowCard:
    now = time.time()
    definition = dict(body.definition or {})
    definition.setdefault("schemaVersion", 1)
    definition.setdefault("name", body.name.strip())
    row = WorkflowCard(
        id=f"wcard_{uuid.uuid4().hex}",
        user_id=user_id,
        created_by=user_id,
        name=body.name.strip(),
        description=body.description.strip(),
        status="draft",
        risk_level=body.risk_level,
        tags_json=_json(sorted({tag.strip() for tag in body.tags if tag.strip()})),
        draft_definition_json=_json(definition),
        created_at=now,
        updated_at=now,
    )
    session.add(row)
    session.commit()
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


def update_card(session: Session, row: WorkflowCard, body) -> WorkflowCard:
    payload = body.model_dump(exclude_unset=True)
    for key in ("name", "description", "risk_level"):
        if key in payload and payload[key] is not None:
            setattr(row, key, str(payload[key]).strip())
    if "tags" in payload and payload["tags"] is not None:
        row.tags_json = _json(sorted({str(tag).strip() for tag in payload["tags"] if str(tag).strip()}))
    if "definition" in payload and payload["definition"] is not None:
        definition = dict(payload["definition"])
        definition.setdefault("schemaVersion", 1)
        row.draft_definition_json = _json(definition)
    # Editing the mutable draft must not make an already-published immutable
    # version unrunnable. Cards without a release remain drafts.
    row.status = "published" if row.latest_version_id else "draft"
    row.updated_at = time.time()
    session.add(row)
    session.commit()
    session.refresh(row)
    return row


def validate_card(row: WorkflowCard, session: Optional[Session] = None) -> Dict[str, Any]:
    compiled = compile_definition(_load(row.draft_definition_json, {}))
    if session is not None and not row.latest_version_id and row.status != "validated":
        row.status = "validated"
        row.updated_at = time.time()
        session.add(row)
        session.commit()
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
        raise WorkflowValidationError([f"publish device is not owned by the current user: {device_id}"])
    if not device.online:
        raise WorkflowValidationError([f"publish device is offline: {device_id}"])
    return device, tool_defs_for_agent(user_id, device_id)


def _live_tool_contracts(
    step_id: str,
    name: str,
    snapshots: List[Tuple[DevicePresence, Dict[str, Any]]],
    errors: List[str],
) -> List[Tuple[DevicePresence, Dict[str, Any], Dict[str, Any], str]]:
    result = []
    for device, live_defs in snapshots:
        live = live_defs.get(name)
        if not live:
            errors.append(f"step {step_id}: tool {name} is not reported by device {device.device_id}")
            continue
        input_schema = live.get("input_schema", {})
        result.append((device, live, input_schema, schema_digest(input_schema)))
    return result


def _resolved_digest(
    step_id: str,
    ref: Dict[str, Any],
    snapshots: List[Tuple[DevicePresence, Dict[str, Any]]],
    live: List[Tuple[DevicePresence, Dict[str, Any], Dict[str, Any], str]],
    errors: List[str],
) -> Optional[str]:
    if snapshots and len(live) != len(snapshots):
        return None
    digests = {item[3] for item in live}
    if len(digests) > 1:
        errors.append(f"step {step_id}: tool {ref['name']} exposes different schemas on bound devices")
        return None
    supplied = str(ref.get("schemaDigest") or "").strip()
    digest = next(iter(digests), supplied)
    if supplied and digest and supplied != digest:
        errors.append(f"step {step_id}: supplied schema digest does not match the current tool")
        return None
    if not snapshots and not supplied:
        errors.append(f"step {step_id}: publish requires device_ids or toolRef.schemaDigest")
        return None
    return supplied or digest


def _frozen_contract(
    name: str,
    ref: Dict[str, Any],
    live: List[Tuple[DevicePresence, Dict[str, Any], Dict[str, Any], str]],
    bound_ids: List[str],
) -> Dict[str, Any]:
    providers = sorted({str(item[0].device_type or "custom") for item in live})
    return {
        "namespace": "device",
        "name": name,
        "schemaDigest": ref["schemaDigest"],
        "inputSchema": live[0][2] if live else ref.get("inputSchema", {}),
        "destructive": any(bool(item[1].get("destructive")) for item in live),
        "provider": providers[0] if len(providers) == 1 else "",
        "providers": providers,
        "publishedDeviceId": bound_ids[0] if len(bound_ids) == 1 else "",
        "publishedDeviceIds": bound_ids,
    }


def _snapshot_contracts(
    session: Session,
    user_id: int,
    definition: Dict[str, Any],
    *,
    device_id: Optional[str] = None,
    device_ids: Optional[List[str]] = None,
) -> Tuple[Dict[str, Any], List[str]]:
    bound_ids = _contract_device_ids(device_id, device_ids)
    snapshots = [_device_snapshot(session, user_id, item) for item in bound_ids]
    contracts: Dict[str, Any] = {}
    errors = []
    for step_id, step in definition["steps"].items():
        if step.get("type") != "mcp":
            continue
        if is_ai_intervention_step(step):
            continue
        ref = step["toolRef"]
        name = str(ref["name"]).strip()
        live_contracts = _live_tool_contracts(step_id, name, snapshots, errors)
        digest = _resolved_digest(step_id, ref, snapshots, live_contracts, errors)
        if digest is None:
            continue
        providers = sorted({str(item[0].device_type or "custom") for item in live_contracts})
        ref["schemaDigest"] = digest
        ref["provider"] = providers[0] if len(providers) == 1 else ""
        contracts[name] = _frozen_contract(name, ref, live_contracts, bound_ids)
    if errors:
        raise WorkflowValidationError(errors)
    return contracts, bound_ids


def publish_card(
    session: Session,
    row: WorkflowCard,
    user_id: int,
    *,
    device_id: Optional[str] = None,
    device_ids: Optional[List[str]] = None,
) -> WorkflowCardVersion:
    compiled = compile_definition(_load(row.draft_definition_json, {}))
    definition = compiled["definition"]
    contracts, bound_ids = _snapshot_contracts(
        session, user_id, definition, device_id=device_id, device_ids=device_ids,
    )
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
    row.status = "published"
    row.updated_at = time.time()
    session.add(row)
    session.commit()
    session.refresh(version)
    return version
