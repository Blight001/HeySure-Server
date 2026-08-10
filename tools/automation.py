"""Unified toolbox MCP for private, AI-owned workflow cards and their runs."""

from __future__ import annotations

import json
import time
import uuid
from typing import Any, Dict, Optional

from fastapi import HTTPException
from sqlmodel import Session, select

from api.core.settings import settings
from api.database import engine
from api.models import (
    DevicePresence,
    WorkflowCard,
    WorkflowCardVersion,
    WorkflowConfirmation,
    WorkflowRun,
    WorkflowStepRun,
)
from api.services.workflows.ai_interaction import create_validated_run, respond_ai_interaction
from api.services.workflows.audit import add_audit
from api.services.workflows.card_service import (
    card_payload,
    create_card,
    delete_card,
    update_card,
    validate_card,
    version_payload,
)
from api.services.workflows.compiler import WorkflowValidationError
from api.services.workflows.run_service import cancel_run, run_payload
from api.services.workflows.schemas import CardCreate, CardUpdate
from api.services.workflows.secrets import decrypt_json
from api.services.workflows.trace import definition_from_trace
from tools.automation_access import (
    _admin_actor,
    _card_visible,
    _creation_tags,
    _is_admin_role,
    _updated_tags,
)



TERMINAL_RUN_STATUSES = {"succeeded", "failed", "cancelled", "timed_out"}
PAUSABLE_RUN_STATUSES = {
    "pending", "running", "retry_wait", "paused_offline",
    "waiting_confirmation", "waiting_ai",
}


def _require_enabled(*, run: bool = False) -> None:
    if not settings.workflow_cards_enabled:
        raise HTTPException(status_code=404, detail="workflow cards are disabled")
    if run and not settings.workflow_scheduler_enabled:
        raise HTTPException(status_code=503, detail="workflow scheduler is disabled")


def _load(raw: str, fallback: Any) -> Any:
    try:
        return json.loads(raw or "")
    except Exception:
        return fallback


def _accessible_card(
    session: Session,
    user_id: int,
    card_id: str,
    ai_config_id: Optional[int],
    *,
    admin_read: bool = False,
) -> WorkflowCard:
    card = session.exec(select(WorkflowCard).where(
        WorkflowCard.id == str(card_id or ""),
        WorkflowCard.user_id == user_id,
        WorkflowCard.deleted_at.is_(None),
    )).first()
    admin_allowed = bool(card and admin_read and _admin_actor(session, user_id, ai_config_id))
    if not card or (not admin_allowed and not _card_visible(card, ai_config_id)):
        raise HTTPException(status_code=404, detail="CARD_NOT_FOUND")
    return card


def _version(session: Session, card: WorkflowCard, version_id: Optional[str]) -> WorkflowCardVersion:
    selected = str(version_id or card.latest_version_id or "")
    row = session.exec(select(WorkflowCardVersion).where(
        WorkflowCardVersion.id == selected,
        WorkflowCardVersion.card_id == card.id,
    )).first()
    if not row:
        raise HTTPException(status_code=404, detail="CARD_VERSION_NOT_FOUND")
    return row


def _compatible_with_device(
    session: Session,
    user_id: int,
    version: Optional[WorkflowCardVersion],
    device_id: str,
) -> bool:
    if not device_id:
        return True
    device = session.exec(select(DevicePresence).where(
        DevicePresence.user_id == user_id,
        DevicePresence.device_id == device_id,
    )).first()
    if not device or not version:
        return False
    contracts = _load(version.tool_contracts_json, {})
    return all(
        not item.get("provider") or item.get("provider") == device.device_type
        for item in contracts.values()
        if isinstance(item, dict)
    )


def _list_cards(user_id: int, args: Dict[str, Any], ai_config_id: Optional[int]) -> Dict[str, Any]:
    query = str(args.get("query") or "").strip().lower()
    wanted_tags = {str(item).strip().lower() for item in args.get("tags", []) if str(item).strip()}
    wanted_status = str(args.get("status") or "").strip()
    device_id = str(args.get("device_id") or "").strip()
    limit = min(100, max(1, int(args.get("limit") or 50)))
    with Session(engine) as session:
        statement = select(WorkflowCard).where(
            WorkflowCard.user_id == user_id,
            WorkflowCard.deleted_at.is_(None),
            WorkflowCard.status != "archived",
        )
        if wanted_status:
            statement = statement.where(WorkflowCard.status == wanted_status)
        rows = session.exec(statement.order_by(WorkflowCard.updated_at.desc())).all()
        admin_allowed = _admin_actor(session, user_id, ai_config_id)
        items = []
        for card in rows:
            if not admin_allowed and not _card_visible(card, ai_config_id):
                continue
            tags = [str(item) for item in _load(card.tags_json, [])]
            lowered = {item.lower() for item in tags}
            if query and query not in f"{card.name} {card.description} {' '.join(tags)}".lower():
                continue
            if wanted_tags and not wanted_tags.issubset(lowered):
                continue
            latest = session.get(WorkflowCardVersion, card.latest_version_id) if card.latest_version_id else None
            if not _compatible_with_device(session, user_id, latest, device_id):
                continue
            payload = card_payload(card)
            payload.pop("definition", None)
            items.append(payload)
            if len(items) >= limit:
                break
        return {"items": items, "count": len(items)}


def _create_card(user_id: int, args: Dict[str, Any], ai_config_id: Optional[int]) -> Dict[str, Any]:
    action = str(args.get("action") or "")
    definition = args.get("definition") if isinstance(args.get("definition"), dict) else {}
    if action == "from_trace":
        definition = definition_from_trace(
            args.get("calls") if isinstance(args.get("calls"), list) else [],
            name=str(args.get("name") or "MCP 轨迹卡片"),
            description=str(args.get("description") or ""),
        )
    with Session(engine) as session:
        owner_id = None if _admin_actor(session, user_id, ai_config_id) else ai_config_id
        body = CardCreate(
            name=str(args.get("name") or ("MCP 轨迹卡片" if action == "from_trace" else "")),
            description=str(args.get("description") or ""),
            tags=_creation_tags(args.get("tags"), owner_id),
            risk_level=str(args.get("risk_level") or ("normal" if action == "from_trace" else "read_only")),
            definition=definition,
            device_id=str(args.get("device_id") or "") or None,
            device_ids=args.get("device_ids") if isinstance(args.get("device_ids"), list) else [],
        )
        return card_payload(create_card(session, user_id, body))


def _clone_card(
    session: Session,
    card: WorkflowCard,
    user_id: int,
    ai_config_id: Optional[int],
) -> Dict[str, Any]:
    source = card_payload(card)
    latest = session.get(WorkflowCardVersion, card.latest_version_id) if card.latest_version_id else None
    definition = _load(latest.definition_json, {}) if latest else source.get("definition") or {}
    body = CardCreate(
        name=f"{card.name}（副本）",
        description=card.description,
        tags=_creation_tags(
            source.get("tags"),
            None if _admin_actor(session, user_id, ai_config_id) else ai_config_id,
        ),
        risk_level=card.risk_level,
        definition=definition,
    )
    return card_payload(create_card(session, user_id, body))


def _edit_card(
    session: Session,
    card: WorkflowCard,
    args: Dict[str, Any],
    user_id: int,
) -> Dict[str, Any]:
    values = {
        key: args[key]
        for key in ("name", "description", "risk_level", "definition", "device_id", "device_ids")
        if key in args
    }
    if "tags" in args:
        values["tags"] = _updated_tags(card, args.get("tags"))
    return card_payload(update_card(session, card, CardUpdate(**values), user_id=user_id))


def _export_card(card: WorkflowCard) -> Dict[str, Any]:
    payload = card_payload(card)
    return {
        "schema": "heysure.workflow-card.export/v1",
        "name": payload["name"],
        "description": payload["description"],
        "tags": payload["tags"],
        "risk_level": payload["risk_level"],
        "definition": payload["definition"],
    }


def _manage_card(user_id: int, args: Dict[str, Any], ai_config_id: Optional[int]) -> Dict[str, Any]:
    action = str(args.get("action") or "").strip().lower()
    if action == "list":
        return _list_cards(user_id, args, ai_config_id)
    if action in {"create", "import", "from_trace"}:
        return _create_card(user_id, args, ai_config_id)
    with Session(engine) as session:
        admin_read = action in {"get", "clone", "validate", "versions", "get_version", "export"}
        card = _accessible_card(
            session,
            user_id,
            str(args.get("card_id") or ""),
            ai_config_id,
            admin_read=admin_read,
        )
        if action == "get":
            payload = card_payload(card)
            if args.get("version_id"):
                payload["version"] = version_payload(_version(session, card, str(args["version_id"])), include_definition=True)
            return payload
        if action in {"edit", "update"}:
            return _edit_card(session, card, args, user_id)
        if action == "clone":
            return _clone_card(session, card, user_id, ai_config_id)
        if action == "delete":
            delete_card(session, card)
            return {"deleted": True, "card_id": card.id}
        if action == "validate":
            return validate_card(card, session)
        if action == "versions":
            rows = session.exec(select(WorkflowCardVersion).where(
                WorkflowCardVersion.card_id == card.id,
            ).order_by(WorkflowCardVersion.version_number.desc())).all()
            return {"items": [version_payload(row) for row in rows]}
        if action == "get_version":
            return version_payload(_version(session, card, str(args.get("version_id") or "")), include_definition=True)
        if action == "export":
            return _export_card(card)
    raise HTTPException(status_code=400, detail="unsupported card action")


def _run_for_ai(
    session: Session,
    user_id: int,
    run_id: str,
    ai_config_id: Optional[int],
    *,
    assigned_interaction: bool = False,
    lock: bool = False,
) -> WorkflowRun:
    statement = select(WorkflowRun).where(
        WorkflowRun.id == str(run_id or ""),
        WorkflowRun.user_id == user_id,
    )
    if lock:
        statement = statement.with_for_update()
    run = session.exec(statement).first()
    if not run:
        raise HTTPException(status_code=404, detail="RUN_NOT_FOUND")
    if not ai_config_id:
        return run
    if run.actor_type == "ai" and run.actor_id == str(ai_config_id):
        return run
    if assigned_interaction:
        pending = session.exec(select(WorkflowConfirmation.id).where(
            WorkflowConfirmation.run_id == run.id,
            WorkflowConfirmation.ai_config_id == int(ai_config_id),
            WorkflowConfirmation.status == "pending",
        )).first()
        if pending:
            return run
    raise HTTPException(status_code=404, detail="RUN_NOT_FOUND")


def _start_run(user_id: int, args: Dict[str, Any], ai_config_id: Optional[int]) -> Dict[str, Any]:
    with Session(engine) as session:
        _accessible_card(
            session,
            user_id,
            str(args.get("card_id") or ""),
            ai_config_id,
            admin_read=True,
        )
        try:
            row = create_validated_run(
                session,
                user_id=user_id,
                card_id=str(args.get("card_id") or ""),
                device_id=str(args.get("device_id") or ""),
                input_value=args.get("input") if isinstance(args.get("input"), dict) else {},
                version_id=str(args.get("version_id") or "") or None,
                idempotency_key=str(args.get("idempotency_key") or "") or None,
                actor=("ai", str(ai_config_id)) if ai_config_id else ("user", str(user_id)),
            )
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc))
        return run_payload(row)


def _list_runs(user_id: int, args: Dict[str, Any], ai_config_id: Optional[int]) -> Dict[str, Any]:
    limit = min(100, max(1, int(args.get("limit") or 50)))
    with Session(engine) as session:
        statement = select(WorkflowRun).where(WorkflowRun.user_id == user_id)
        if ai_config_id:
            statement = statement.where(
                WorkflowRun.actor_type == "ai",
                WorkflowRun.actor_id == str(ai_config_id),
            )
        if args.get("card_id"):
            card = _accessible_card(session, user_id, str(args["card_id"]), ai_config_id)
            statement = statement.where(WorkflowRun.card_id == card.id)
        if args.get("status"):
            statement = statement.where(WorkflowRun.status == str(args["status"]))
        rows = session.exec(statement.order_by(WorkflowRun.created_at.desc()).limit(limit)).all()
        return {"items": [run_payload(row) for row in rows], "count": len(rows)}


def _pause_run(user_id: int, args: Dict[str, Any], ai_config_id: Optional[int]) -> Dict[str, Any]:
    with Session(engine) as session:
        run = _run_for_ai(session, user_id, str(args.get("run_id") or ""), ai_config_id, lock=True)
        if run.status == "paused":
            return run_payload(run)
        if run.status not in PAUSABLE_RUN_STATUSES:
            raise HTTPException(status_code=409, detail="RUN_NOT_PAUSABLE")
        busy = session.exec(select(WorkflowStepRun.id).where(
            WorkflowStepRun.run_id == run.id,
            WorkflowStepRun.status.in_(["dispatching", "waiting_device"]),
        )).first()
        if busy:
            raise HTTPException(status_code=409, detail="RUN_BUSY_CANNOT_PAUSE")
        now = time.time()
        variables = _load(run.variables_json, {"steps": {}})
        variables["_automation_control"] = {"paused_from": run.status, "paused_at": now}
        previous = run.status
        run.variables_json = json.dumps(variables, ensure_ascii=False)
        run.status = "paused"
        run.next_wakeup_at = None
        run.updated_at = now
        run.lock_version += 1
        session.add(run)
        add_audit(session, event_type="run_paused", run=run, status_from=previous, status_to="paused")
        session.commit()
        session.refresh(run)
        return run_payload(run)


def _resume_run(user_id: int, args: Dict[str, Any], ai_config_id: Optional[int]) -> Dict[str, Any]:
    with Session(engine) as session:
        run = _run_for_ai(session, user_id, str(args.get("run_id") or ""), ai_config_id, lock=True)
        if run.status != "paused":
            raise HTTPException(status_code=409, detail="RUN_NOT_PAUSED")
        now = time.time()
        variables = _load(run.variables_json, {"steps": {}})
        control = variables.pop("_automation_control", {})
        paused_at = float(control.get("paused_at") or now)
        shift = max(0.0, now - paused_at)
        restored = str(control.get("paused_from") or "pending")
        if restored not in PAUSABLE_RUN_STATUSES:
            restored = "pending"
        run.deadline_at += shift
        run.status = restored
        run.variables_json = json.dumps(variables, ensure_ascii=False)
        run.next_wakeup_at = now if restored in {"pending", "running", "retry_wait", "paused_offline"} else None
        run.updated_at = now
        run.lock_version += 1
        steps = session.exec(select(WorkflowStepRun).where(
            WorkflowStepRun.run_id == run.id,
            WorkflowStepRun.status.notin_(["succeeded", "failed", "timed_out", "cancelled"]),
        )).all()
        for step in steps:
            step.deadline_at += shift
            session.add(step)
        confirmations = session.exec(select(WorkflowConfirmation).where(
            WorkflowConfirmation.run_id == run.id,
            WorkflowConfirmation.status == "pending",
        )).all()
        for item in confirmations:
            item.expires_at += shift
            session.add(item)
        session.add(run)
        add_audit(session, event_type="run_resumed", run=run, status_from="paused", status_to=restored)
        session.commit()
        session.refresh(run)
        return run_payload(run)


def _manage_run(user_id: int, args: Dict[str, Any], ai_config_id: Optional[int]) -> Dict[str, Any]:
    action = str(args.get("action") or "").strip().lower()
    if action in {"start", "run"}:
        return _start_run(user_id, args, ai_config_id)
    if action == "list_runs":
        return _list_runs(user_id, args, ai_config_id)
    if action == "pause":
        return _pause_run(user_id, args, ai_config_id)
    if action == "resume":
        return _resume_run(user_id, args, ai_config_id)
    with Session(engine) as session:
        run = _run_for_ai(
            session,
            user_id,
            str(args.get("run_id") or ""),
            ai_config_id,
            assigned_interaction=action == "respond",
            lock=action in {"cancel", "retry", "respond"},
        )
        if action == "status":
            return run_payload(run)
        if action == "cancel":
            return run_payload(cancel_run(session, run, str(args.get("reason") or "cancelled by AI")))
        if action == "retry":
            try:
                return run_payload(create_validated_run(
                    session,
                    user_id=user_id,
                    card_id=run.card_id,
                    device_id=run.device_id,
                    input_value=decrypt_json(run.input_json),
                    version_id=run.card_version_id,
                    idempotency_key=str(args.get("idempotency_key") or "") or f"retry:{run.id}:{uuid.uuid4().hex}",
                    actor=("ai", str(ai_config_id)) if ai_config_id else ("user", str(user_id)),
                ))
            except ValueError as exc:
                raise HTTPException(status_code=409, detail=str(exc))
        if action == "respond":
            if not ai_config_id:
                raise HTTPException(status_code=403, detail="AI_INTERACTION_REQUIRES_AI")
            try:
                return run_payload(respond_ai_interaction(
                    session,
                    run=run,
                    user_id=user_id,
                    ai_config_id=int(ai_config_id),
                    approved=bool(args.get("approved")),
                    parameters=args.get("parameters") if isinstance(args.get("parameters"), dict) else {},
                    message=str(args.get("message") or ""),
                ))
            except ValueError as exc:
                raise HTTPException(status_code=409, detail=str(exc))
    raise HTTPException(status_code=400, detail="unsupported run action")


def _automation_manage(user_id: int, args: Dict[str, Any], ai_config_id: Optional[int]) -> Dict[str, Any]:
    action = str(args.get("action") or "").strip().lower()
    _require_enabled(run=action in {"start", "run", "retry", "resume", "respond"})
    try:
        if action in {
            "list", "get", "create", "import", "from_trace", "clone", "edit", "update",
            "delete", "validate", "versions", "get_version", "export",
        }:
            return _manage_card(user_id, args, ai_config_id)
        if action in {
            "start", "run", "list_runs", "status", "pause", "resume",
            "cancel", "retry", "respond",
        }:
            return _manage_run(user_id, args, ai_config_id)
    except WorkflowValidationError as exc:
        raise HTTPException(
            status_code=422,
            detail={"code": "CARD_VALIDATION_FAILED", "errors": exc.errors, "warnings": exc.warnings},
        )
    raise HTTPException(status_code=400, detail="unsupported automation.manage action")


AUTOMATION_MANAGE_SCHEMA = {
    "type": "object",
    "properties": {
        "action": {
            "type": "string",
            "enum": [
                "list", "get", "create", "import", "from_trace", "clone", "edit", "delete",
                "validate", "versions", "get_version", "export", "start",
                "list_runs", "status", "pause", "resume", "cancel", "retry", "respond",
            ],
            "description": "唯一动作选择器；start 启动，pause/resume 暂停恢复，edit 编辑卡片。",
        },
        "card_id": {"type": "string"},
        "version_id": {"type": "string"},
        "run_id": {"type": "string"},
        "name": {"type": "string"},
        "description": {"type": "string"},
        "tags": {"type": "array", "items": {"type": "string"}},
        "risk_level": {"type": "string"},
        "definition": {"type": "object"},
        "calls": {"type": "array", "minItems": 1, "maxItems": 50, "items": {"type": "object"}},
        "device_id": {"type": "string"},
        "device_ids": {"type": "array", "items": {"type": "string"}, "maxItems": 20},
        "input": {"type": "object"},
        "idempotency_key": {"type": "string"},
        "query": {"type": "string"},
        "status": {"type": "string"},
        "limit": {"type": "integer", "minimum": 1, "maximum": 100},
        "reason": {"type": "string"},
        "approved": {"type": "boolean"},
        "parameters": {"type": "object"},
        "message": {"type": "string"},
    },
    "required": ["action"],
}
