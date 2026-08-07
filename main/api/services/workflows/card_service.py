"""CRUD, validation and immutable publishing for workflow cards."""

from __future__ import annotations

import json
import time
import uuid
from typing import Any, Dict, Optional

from sqlmodel import Session, select

from api.devices.presence import tool_defs_for_agent
from api.models import WorkflowCard, WorkflowCardVersion

from .compiler import WorkflowValidationError, compile_definition, definition_digest, schema_digest


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
        )
    ).first()


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


def validate_card(row: WorkflowCard) -> Dict[str, Any]:
    compiled = compile_definition(_load(row.draft_definition_json, {}))
    return {"valid": True, "digest": compiled["digest"], "warnings": compiled["warnings"]}


def _snapshot_contracts(user_id: int, device_id: Optional[str], definition: Dict[str, Any]) -> Dict[str, Any]:
    live_defs = tool_defs_for_agent(user_id, device_id) if device_id else {}
    contracts: Dict[str, Any] = {}
    errors = []
    for step_id, step in definition["steps"].items():
        if step.get("type") != "mcp":
            continue
        ref = step["toolRef"]
        name = str(ref["name"]).strip()
        live = live_defs.get(name)
        supplied_digest = str(ref.get("schemaDigest") or "").strip()
        if device_id and not live:
            errors.append(f"step {step_id}: tool {name} is not reported by device {device_id}")
            continue
        input_schema = live.get("input_schema", {}) if live else ref.get("inputSchema", {})
        digest = schema_digest(input_schema)
        if supplied_digest and supplied_digest != digest:
            errors.append(f"step {step_id}: supplied schema digest does not match the current tool")
            continue
        if not device_id and not supplied_digest:
            errors.append(f"step {step_id}: publish requires device_id or toolRef.schemaDigest")
            continue
        ref["schemaDigest"] = supplied_digest or digest
        contracts[name] = {
            "namespace": "device",
            "name": name,
            "schemaDigest": ref["schemaDigest"],
            "inputSchema": input_schema,
            "destructive": bool(live.get("destructive")) if live else False,
        }
    if errors:
        raise WorkflowValidationError(errors)
    return contracts


def publish_card(
    session: Session, row: WorkflowCard, user_id: int, *, device_id: Optional[str] = None
) -> WorkflowCardVersion:
    compiled = compile_definition(_load(row.draft_definition_json, {}))
    definition = compiled["definition"]
    contracts = _snapshot_contracts(user_id, device_id, definition)
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
