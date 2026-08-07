"""Stable MCP surface for discovering and running workflow cards."""

from __future__ import annotations

import json
from typing import Any, Dict, Optional

from fastapi import HTTPException
from sqlmodel import Session, select

from api.core.settings import settings
from api.database import engine
from api.models import WorkflowCard, WorkflowCardVersion, WorkflowRun
from api.services.workflows.card_service import (
    card_payload,
    create_card,
    owned_card,
    publish_card,
    update_card,
    validate_card,
    version_payload,
)
from api.services.workflows.compiler import WorkflowValidationError
from api.services.workflows.trace import definition_from_trace
from api.services.workflows.run_service import cancel_run, create_run, run_payload
from api.services.workflows.schemas import CardCreate, CardUpdate


def _require_enabled(run: bool = False) -> None:
    if not settings.workflow_cards_enabled:
        raise HTTPException(status_code=404, detail="workflow cards are disabled")
    if run and not settings.workflow_scheduler_enabled:
        raise HTTPException(status_code=503, detail="workflow scheduler is disabled")


def _load(raw: str, fallback):
    try:
        return json.loads(raw or "")
    except Exception:
        return fallback


def _automation_list(user_id: int, args: Dict[str, Any], ai_config_id: Optional[int]) -> Dict[str, Any]:
    _require_enabled()
    query = str(args.get("query") or "").strip().lower()
    tags = {str(item).strip().lower() for item in args.get("tags", []) if str(item).strip()}
    device_id = str(args.get("device_id") or "").strip()
    limit = min(100, max(1, int(args.get("limit") or 20)))
    with Session(engine) as session:
        rows = session.exec(
            select(WorkflowCard).where(
                WorkflowCard.user_id == user_id,
                WorkflowCard.status == "published",
                WorkflowCard.deleted_at.is_(None),
            ).order_by(WorkflowCard.updated_at.desc())
        ).all()
        items = []
        for row in rows:
            row_tags = {str(item).lower() for item in _load(row.tags_json, [])}
            if query and query not in f"{row.name} {row.description} {' '.join(row_tags)}".lower():
                continue
            if tags and not tags.issubset(row_tags):
                continue
            version = session.get(WorkflowCardVersion, row.latest_version_id) if row.latest_version_id else None
            if not version:
                continue
            definition = _load(version.definition_json, {})
            contracts = _load(version.tool_contracts_json, {})
            if device_id:
                from api.models import DevicePresence
                device = session.exec(
                    select(DevicePresence).where(
                        DevicePresence.user_id == user_id,
                        DevicePresence.device_id == device_id,
                    )
                ).first()
                if not device or any(
                    contract.get("provider") and contract.get("provider") != device.device_type
                    for contract in contracts.values()
                ):
                    continue
            items.append({
                "id": row.id,
                "name": row.name,
                "description": row.description,
                "tags": sorted(row_tags),
                "risk_level": row.risk_level,
                "version_id": version.id,
                "version": version.version_number,
                "input_schema": definition.get("inputSchema", {"type": "object"}),
                "required_capabilities": sorted(contracts),
                "compatible_device_id": device_id or None,
            })
            if len(items) >= limit:
                break
    return {"items": items, "count": len(items)}


def _automation_get(user_id: int, args: Dict[str, Any], ai_config_id: Optional[int]) -> Dict[str, Any]:
    _require_enabled()
    card_id = str(args.get("card_id") or "").strip()
    with Session(engine) as session:
        card = owned_card(session, user_id, card_id)
        if not card:
            raise HTTPException(status_code=404, detail="CARD_NOT_FOUND")
        version_id = str(args.get("version_id") or card.latest_version_id or "")
        version = session.exec(
            select(WorkflowCardVersion).where(
                WorkflowCardVersion.id == version_id,
                WorkflowCardVersion.card_id == card.id,
            )
        ).first()
        if not version:
            raise HTTPException(status_code=404, detail="CARD_VERSION_NOT_FOUND")
        definition = _load(version.definition_json, {})
        return {
            "id": card.id,
            "name": card.name,
            "description": card.description,
            "status": card.status,
            "risk_level": card.risk_level,
            "tags": _load(card.tags_json, []),
            "version": version_payload(version),
            "input_schema": definition.get("inputSchema", {"type": "object"}),
            "required_capabilities": sorted(_load(version.tool_contracts_json, {})),
            "limits": definition.get("limits", {}),
        }


def _automation_run(user_id: int, args: Dict[str, Any], ai_config_id: Optional[int]) -> Dict[str, Any]:
    _require_enabled(run=True)
    with Session(engine) as session:
        try:
            row = create_run(
                session,
                user_id=user_id,
                card_id=str(args.get("card_id") or ""),
                device_id=str(args.get("device_id") or ""),
                input_value=args.get("input") if isinstance(args.get("input"), dict) else {},
                version_id=str(args.get("version_id") or "") or None,
                idempotency_key=str(args.get("idempotency_key") or "") or None,
                actor_type="ai",
                actor_id=str(ai_config_id or ""),
            )
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc))
        return run_payload(row)


def _automation_status(user_id: int, args: Dict[str, Any], ai_config_id: Optional[int]) -> Dict[str, Any]:
    _require_enabled()
    with Session(engine) as session:
        row = session.exec(
            select(WorkflowRun).where(
                WorkflowRun.id == str(args.get("run_id") or ""),
                WorkflowRun.user_id == user_id,
            )
        ).first()
        if not row:
            raise HTTPException(status_code=404, detail="RUN_NOT_FOUND")
        return run_payload(row)


def _automation_cancel(user_id: int, args: Dict[str, Any], ai_config_id: Optional[int]) -> Dict[str, Any]:
    _require_enabled()
    with Session(engine) as session:
        row = session.exec(
            select(WorkflowRun).where(
                WorkflowRun.id == str(args.get("run_id") or ""),
                WorkflowRun.user_id == user_id,
            )
        ).first()
        if not row:
            raise HTTPException(status_code=404, detail="RUN_NOT_FOUND")
        return run_payload(cancel_run(session, row, str(args.get("reason") or "cancelled by AI")))


def _automation_manage(user_id: int, args: Dict[str, Any], ai_config_id: Optional[int]) -> Dict[str, Any]:
    _require_enabled()
    action = str(args.get("action") or "").strip()
    with Session(engine) as session:
        if action == "create":
            row = create_card(session, user_id, CardCreate(
                name=str(args.get("name") or ""),
                description=str(args.get("description") or ""),
                tags=args.get("tags") if isinstance(args.get("tags"), list) else [],
                risk_level=str(args.get("risk_level") or "read_only"),
                definition=args.get("definition") if isinstance(args.get("definition"), dict) else {},
            ))
            return card_payload(row)
        if action == "from_trace":
            calls = args.get("calls") if isinstance(args.get("calls"), list) else []
            definition = definition_from_trace(
                calls,
                name=str(args.get("name") or "MCP 轨迹草稿"),
                description=str(args.get("description") or ""),
            )
            row = create_card(session, user_id, CardCreate(
                name=str(args.get("name") or "MCP 轨迹草稿"),
                description=str(args.get("description") or ""),
                tags=args.get("tags") if isinstance(args.get("tags"), list) else [],
                risk_level=str(args.get("risk_level") or "normal"),
                definition=definition,
            ))
            return card_payload(row)
        card = owned_card(session, user_id, str(args.get("card_id") or ""))
        if not card:
            raise HTTPException(status_code=404, detail="CARD_NOT_FOUND")
        try:
            if action == "update":
                body = CardUpdate(**{
                    key: args[key]
                    for key in ("name", "description", "tags", "risk_level", "definition")
                    if key in args
                })
                return card_payload(update_card(session, card, body))
            if action == "validate":
                return validate_card(card, session)
            if action == "publish":
                return version_payload(
                    publish_card(session, card, user_id, device_id=str(args.get("device_id") or "") or None),
                    include_definition=False,
                )
        except WorkflowValidationError as exc:
            raise HTTPException(status_code=422, detail={"code": "CARD_VALIDATION_FAILED", "errors": exc.errors})
    raise HTTPException(status_code=400, detail="unsupported automation.manage action")


AUTOMATION_LIST_SCHEMA = {
    "type": "object",
    "properties": {
        "query": {"type": "string"},
        "tags": {"type": "array", "items": {"type": "string"}},
        "device_id": {"type": "string"},
        "limit": {"type": "integer", "minimum": 1, "maximum": 100},
    },
}
AUTOMATION_GET_SCHEMA = {
    "type": "object",
    "properties": {"card_id": {"type": "string"}, "version_id": {"type": "string"}},
    "required": ["card_id"],
}
AUTOMATION_RUN_SCHEMA = {
    "type": "object",
    "properties": {
        "card_id": {"type": "string"}, "version_id": {"type": "string"},
        "device_id": {"type": "string"}, "input": {"type": "object"},
        "idempotency_key": {"type": "string"},
    },
    "required": ["card_id", "device_id", "input", "idempotency_key"],
}
AUTOMATION_STATUS_SCHEMA = {
    "type": "object", "properties": {"run_id": {"type": "string"}}, "required": ["run_id"]
}
AUTOMATION_CANCEL_SCHEMA = {
    "type": "object",
    "properties": {"run_id": {"type": "string"}, "reason": {"type": "string"}},
    "required": ["run_id"],
}
AUTOMATION_MANAGE_SCHEMA = {
    "type": "object",
    "properties": {
        "action": {"type": "string", "enum": ["create", "from_trace", "update", "validate", "publish"]},
        "card_id": {"type": "string"}, "name": {"type": "string"},
        "description": {"type": "string"}, "tags": {"type": "array", "items": {"type": "string"}},
        "risk_level": {"type": "string"}, "definition": {"type": "object"},
        "device_id": {"type": "string"},
        "calls": {"type": "array", "minItems": 1, "maxItems": 50, "items": {"type": "object"}},
    },
    "required": ["action"],
}
