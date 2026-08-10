"""AI-mediated workflow steps, callbacks, and durable notifications."""

from __future__ import annotations

import json
import time
import uuid
from dataclasses import dataclass
from typing import Any, Dict, Optional, Tuple

from sqlmodel import Session, select

from api.core.settings import settings
from api.models import (
    AssistantAIConfig,
    DevicePresence,
    WorkflowCard,
    WorkflowCardVersion,
    WorkflowConfirmation,
    WorkflowRun,
)

from .audit import add_audit
from .interaction_steps import AI_INTERVENTION_TOOL, is_ai_intervention_step
from .permissions import WorkflowDispatchError, validate_run_device
from .run_service import _redact, advance_run, confirmation_payload, create_run, error_payload, fail_run
from .secrets import decrypt_json


INTERACTION_TYPES = {"ai_review", "user_via_ai"}
ACTIVE_RUN_STATUSES = {
    "pending", "running", "waiting_device", "waiting_confirmation", "waiting_ai",
    "retry_wait", "paused_offline",
}


@dataclass(frozen=True)
class InteractionSpec:
    kind: str
    step_id: str
    prompt: str
    next_step_id: str
    denied_step_id: str
    save_as: str
    timeout_seconds: int


def _load(raw: str, fallback: Any) -> Any:
    try:
        return json.loads(raw or "")
    except Exception:
        return fallback


def _dump(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def _interaction_spec(step_id: str, step: Dict[str, Any]) -> Optional[InteractionSpec]:
    if step.get("type") == "confirm":
        return InteractionSpec(
            kind="user_via_ai",
            step_id=step_id,
            prompt=str(step.get("message") or step.get("riskSummary") or "请确认是否继续执行自动化卡片"),
            next_step_id=str(step.get("next") or ""),
            denied_step_id=str(step.get("onDenied") or ""),
            save_as="",
            timeout_seconds=int(step.get("timeoutSeconds", 300)),
        )
    if not is_ai_intervention_step(step):
        return None
    arguments = step.get("arguments") if isinstance(step.get("arguments"), dict) else {}
    return InteractionSpec(
        kind="ai_review",
        step_id=step_id,
        prompt=str(arguments.get("prompt") or "请核对当前流程信息并决定是否继续"),
        next_step_id=str(step.get("next") or ""),
        denied_step_id=str(step.get("onError") or "") if step.get("onError") != "fail" else "",
        save_as=str(step.get("saveAs") or ""),
        timeout_seconds=int(step.get("timeoutSeconds", 300)),
    )


def _run_step(session: Session, run: WorkflowRun) -> Optional[InteractionSpec]:
    version = session.get(WorkflowCardVersion, run.card_version_id)
    definition = _load(version.definition_json, {}) if version else {}
    step = definition.get("steps", {}).get(run.current_step_id)
    return _interaction_spec(run.current_step_id, step) if isinstance(step, dict) else None


def _interaction_ai_id(session: Session, run: WorkflowRun) -> Optional[int]:
    candidate: Optional[int] = None
    if run.actor_type == "ai":
        try:
            value = int(run.actor_id)
            if value > 0:
                candidate = value
        except (TypeError, ValueError):
            pass
    if candidate is None:
        device = session.exec(select(DevicePresence).where(
            DevicePresence.user_id == run.user_id,
            DevicePresence.device_id == run.device_id,
        )).first()
        candidate = int(device.ai_config_id) if device and device.ai_config_id else None
    config = session.exec(select(AssistantAIConfig).where(
        AssistantAIConfig.user_id == run.user_id,
        AssistantAIConfig.id == candidate,
    )).first() if candidate else None
    return int(config.id) if config else None


def _request_interaction(
    session: Session,
    run: WorkflowRun,
    spec: InteractionSpec,
) -> WorkflowConfirmation:
    now = time.time()
    row = WorkflowConfirmation(
        id=f"wconf_{uuid.uuid4().hex}",
        run_id=run.id,
        step_id=spec.step_id,
        confirmation_type=spec.kind,
        risk_summary=spec.prompt[:2000],
        next_step_id=spec.next_step_id,
        on_denied_step_id=spec.denied_step_id,
        requested_user_id=run.user_id,
        ai_config_id=_interaction_ai_id(session, run),
        save_as=spec.save_as,
        expires_at=min(run.deadline_at, now + max(1, spec.timeout_seconds)),
    )
    if not row.ai_config_id:
        raise ValueError("AI_INTERACTION_UNAVAILABLE")
    previous = run.status
    run.status = "waiting_ai"
    run.next_wakeup_at = row.expires_at
    run.updated_at = now
    run.lock_version += 1
    session.add(row)
    session.add(run)
    add_audit(
        session,
        event_type="ai_interaction_requested",
        run=run,
        step_id=spec.step_id,
        status_from=previous,
        status_to="waiting_ai",
        detail={"confirmation_id": row.id, "type": spec.kind, "prompt": spec.prompt[:500]},
    )
    return row


def _enter_interaction(session: Session, run_id: str) -> Optional[WorkflowRun]:
    run = session.exec(select(WorkflowRun).where(
        WorkflowRun.id == run_id,
    ).with_for_update(skip_locked=True)).first()
    if not run or run.status not in {"pending", "running", "retry_wait"}:
        return run
    spec = _run_step(session, run)
    if not spec:
        return run
    if time.time() >= run.deadline_at:
        fail_run(session, run, error_payload("RUN_TIMEOUT", "workflow deadline elapsed", "interaction"), status="timed_out")
    else:
        run.started_at = run.started_at or time.time()
        run.transition_count += 1
        try:
            _request_interaction(session, run, spec)
        except ValueError as exc:
            fail_run(session, run, error_payload(str(exc), "no AI is available for workflow interaction", "interaction"))
    session.commit()
    return run


def advance_interactive_run(session: Session, run_id: str) -> Optional[WorkflowRun]:
    """Advance normal steps normally and intercept AI/user interaction steps."""
    run = session.get(WorkflowRun, run_id)
    if run and _run_step(session, run):
        return _enter_interaction(session, run_id)
    return advance_run(session, run_id)


def _version_for_run_creation(
    session: Session,
    user_id: int,
    card_id: str,
    version_id: Optional[str],
) -> Optional[WorkflowCardVersion]:
    card = session.exec(select(WorkflowCard).where(
        WorkflowCard.id == card_id,
        WorkflowCard.user_id == user_id,
        WorkflowCard.deleted_at.is_(None),
    )).first()
    selected_id = version_id or (card.latest_version_id if card else None)
    if not card or not selected_id:
        return None
    return session.exec(select(WorkflowCardVersion).where(
        WorkflowCardVersion.id == selected_id,
        WorkflowCardVersion.card_id == card.id,
    )).first()


def _validate_concurrency(session: Session, user_id: int, device_id: str) -> None:
    active = session.exec(select(WorkflowRun).where(
        WorkflowRun.user_id == user_id,
        WorkflowRun.status.in_(ACTIVE_RUN_STATUSES),
    )).all()
    if len(active) >= int(settings.workflow_max_concurrent_per_user):
        raise ValueError("RUN_CONCURRENCY_LIMIT")
    if sum(item.device_id == device_id for item in active) >= int(settings.workflow_max_concurrent_per_device):
        raise ValueError("DEVICE_CONCURRENCY_LIMIT")


def create_validated_run(
    session: Session,
    *,
    user_id: int,
    card_id: str,
    device_id: str,
    input_value: Dict[str, Any],
    version_id: Optional[str] = None,
    idempotency_key: Optional[str] = None,
    actor: Tuple[str, str] = ("user", ""),
) -> WorkflowRun:
    if idempotency_key:
        existing = session.exec(select(WorkflowRun).where(
            WorkflowRun.user_id == user_id,
            WorkflowRun.idempotency_key == idempotency_key,
        )).first()
        if existing:
            return existing
    version = _version_for_run_creation(session, user_id, card_id, version_id)
    if version:
        try:
            validate_run_device(
                session,
                user_id=user_id,
                device_id=device_id,
                definition=_load(version.definition_json, {}),
                version=version,
            )
        except WorkflowDispatchError as exc:
            raise ValueError(f"{exc.code}: {exc}") from exc
        _validate_concurrency(session, user_id, device_id)
    return create_run(
        session,
        user_id=user_id,
        card_id=card_id,
        device_id=device_id,
        input_value=input_value,
        version_id=version_id,
        idempotency_key=idempotency_key,
        actor_type=actor[0],
        actor_id=actor[1],
    )


def retry_validated_run(
    session: Session,
    *,
    run: WorkflowRun,
    user_id: int,
    idempotency_key: Optional[str] = None,
) -> WorkflowRun:
    if run.status not in {"failed", "timed_out", "cancelled"}:
        raise ValueError("RUN_NOT_RETRYABLE")
    return create_validated_run(
        session,
        user_id=user_id,
        card_id=run.card_id,
        device_id=run.device_id,
        input_value=decrypt_json(run.input_json),
        version_id=run.card_version_id,
        idempotency_key=idempotency_key or f"retry:{run.id}:{uuid.uuid4().hex}",
    )


def _apply_approved_response(run: WorkflowRun, item: WorkflowConfirmation, response: Dict[str, Any]) -> None:
    if item.confirmation_type == "ai_review" and item.save_as:
        variables = _load(run.variables_json, {"steps": {}})
        variables.setdefault("steps", {})[item.save_as] = {"result": response["parameters"], "error": None}
        run.variables_json = _dump(variables)
    run.current_step_id = item.next_step_id
    run.status = "running"


def _apply_denied_response(session: Session, run: WorkflowRun, item: WorkflowConfirmation) -> None:
    if item.on_denied_step_id:
        run.current_step_id = item.on_denied_step_id
        run.status = "running"
        return
    fail_run(
        session,
        run,
        error_payload("AI_INTERACTION_DENIED", "workflow interaction was denied or expired", "interaction"),
        status="cancelled",
    )


def respond_ai_interaction(
    session: Session,
    *,
    run: WorkflowRun,
    user_id: int,
    ai_config_id: int,
    approved: bool,
    parameters: Optional[Dict[str, Any]] = None,
    message: str = "",
) -> WorkflowRun:
    item = session.exec(select(WorkflowConfirmation).where(
        WorkflowConfirmation.run_id == run.id,
        WorkflowConfirmation.status == "pending",
        WorkflowConfirmation.ai_config_id == ai_config_id,
    ).order_by(WorkflowConfirmation.created_at.desc()).with_for_update(skip_locked=True)).first()
    if not item:
        raise ValueError("AI_INTERACTION_NOT_PENDING")
    if run.user_id != user_id:
        raise ValueError("AI_INTERACTION_ACCESS_DENIED")
    now = time.time()
    approved = bool(approved and now < item.expires_at)
    response = {"parameters": _redact(parameters or {}), "message": str(message or "")[:2000]}
    item.status = "approved" if approved else "denied"
    item.decision = item.status
    item.response_json = _dump(response)
    item.decided_at = now
    session.add(item)
    _apply_approved_response(run, item, response) if approved else _apply_denied_response(session, run, item)
    if run.status not in {"failed", "cancelled", "timed_out"}:
        run.next_wakeup_at = now
        run.updated_at = now
        run.lock_version += 1
        session.add(run)
    add_audit(
        session,
        event_type="ai_interaction_decided",
        run=run,
        step_id=item.step_id,
        status_from="waiting_ai",
        status_to=run.status,
        detail={"confirmation_id": item.id, "decision": item.status},
    )
    session.commit()
    session.refresh(run)
    return run


def interaction_confirmation_payload(row: WorkflowConfirmation) -> Dict[str, Any]:
    payload = confirmation_payload(row)
    payload["ai_config_id"] = row.ai_config_id
    payload["notified_at"] = row.notified_at
    return payload


def expire_ai_interactions(session: Session, now: Optional[float] = None, limit: int = 100) -> int:
    current = float(now or time.time())
    items = session.exec(select(WorkflowConfirmation).where(
        WorkflowConfirmation.status == "pending",
        WorkflowConfirmation.confirmation_type.in_(INTERACTION_TYPES),
        WorkflowConfirmation.expires_at <= current,
    ).limit(limit)).all()
    for item in items:
        run = session.get(WorkflowRun, item.run_id)
        item.status = "expired"
        item.decision = "expired"
        item.decided_at = current
        session.add(item)
        if not run or run.status != "waiting_ai":
            continue
        if item.on_denied_step_id:
            run.current_step_id = item.on_denied_step_id
            run.status = "running"
            run.next_wakeup_at = current
            run.updated_at = current
            run.lock_version += 1
            session.add(run)
        else:
            fail_run(
                session,
                run,
                error_payload("AI_INTERACTION_TIMEOUT", "workflow AI interaction expired", "interaction"),
                status="cancelled",
            )
        add_audit(
            session,
            event_type="ai_interaction_expired",
            run=run,
            step_id=item.step_id,
            status_from="waiting_ai",
            status_to=run.status,
        )
    if items:
        session.commit()
    return len(items)

