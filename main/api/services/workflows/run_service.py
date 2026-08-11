"""Transactional state transitions for deterministic workflow runs."""

from __future__ import annotations

import json
import time
import uuid
from typing import Any, Dict, NamedTuple, Optional

from jsonschema import Draft202012Validator
from sqlmodel import Session, select

from api.models import (
    DevicePresence,
    WorkflowCard,
    WorkflowCardVersion,
    WorkflowConfirmation,
    WorkflowRun,
    WorkflowStepRun,
)

from api.core.settings import settings
from .audit import add_audit
from .expression import evaluate_expression, render_template, resolve_target_arguments
from .result_store import device_step_error, save_result
from .secrets import decrypt_json, encrypt_json
from .step_device_binding import step_contract, step_device_id
from .workflow_cancellation import (
    RUN_CONFIRMATION_SCOPE,
    cancel_workflow_run,
    confirmation_granted,
    decide_confirmation,
    expire_confirmations,
    renew_dispatch_step_deadline,
    request_confirmation,
)


TERMINAL_RUN_STATUSES = {"succeeded", "failed", "cancelled", "timed_out"}
SENSITIVE_KEYS = {"authorization", "cookie", "password", "secret", "token", "api_key", "apikey"}
MAX_PROJECTED_RESULT_BYTES = 64 * 1024


class RunActorContext(NamedTuple):
    actor_type: str = "user"
    actor_id: str = ""
    initial_variables: Optional[Dict[str, Any]] = None


def _dump(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), default=str)


def _load(raw: Optional[str], fallback: Any) -> Any:
    try:
        return json.loads(raw or "")
    except Exception:
        return fallback


def error_payload(code: str, message: str, phase: str, retryable: bool = False) -> Dict[str, Any]:
    return {"code": code, "message": message, "phase": phase, "retryable": retryable}


def _redact(value: Any, depth: int = 0) -> Any:
    if depth > 16:
        return "[TRUNCATED]"
    if isinstance(value, dict):
        return {
            str(key): "[REDACTED]" if str(key).lower() in SENSITIVE_KEYS else _redact(child, depth + 1)
            for key, child in value.items()
        }
    if isinstance(value, list):
        return [_redact(child, depth + 1) for child in value[:1000]]
    return value


def _project_result(result: Any, projection: Any) -> Any:
    if projection is None:
        return result
    output: Dict[str, Any] = {}
    for path in projection:
        current = result
        parts = str(path).split(".")
        for part in parts:
            if part.startswith("__") or not isinstance(current, dict) or part not in current:
                raise ValueError(f"result projection path is unavailable: {path}")
            current = current[part]
        target = output
        for part in parts[:-1]:
            target = target.setdefault(part, {})
        target[parts[-1]] = current
    return output


def _context(run: WorkflowRun, device: Optional[DevicePresence] = None) -> Dict[str, Any]:
    variables = _load(run.variables_json, {})
    return {
        "input": decrypt_json(run.input_json),
        "steps": variables.get("steps", {}),
        "run": {"id": run.id, "startedAt": run.started_at, "createdAt": run.created_at},
        "device": {
            "id": getattr(device, "device_id", run.device_id) if device else run.device_id,
            "type": getattr(device, "device_type", "") if device else "",
            "platform": getattr(device, "platform", "") if device else "",
        },
    }


def run_payload(row: WorkflowRun) -> Dict[str, Any]:
    return {
        "id": row.id,
        "card_id": row.card_id,
        "card_version_id": row.card_version_id,
        "device_id": row.device_id,
        "status": row.status,
        "current_step_id": row.current_step_id,
        "transition_count": row.transition_count,
        "output": _load(row.output_json, None),
        "error": _load(row.error_json, None),
        "deadline_at": row.deadline_at,
        "started_at": row.started_at,
        "finished_at": row.finished_at,
        "created_at": row.created_at,
        "updated_at": row.updated_at,
        "actor_type": row.actor_type,
        "actor_id": row.actor_id,
    }


def step_payload(row: WorkflowStepRun) -> Dict[str, Any]:
    return {
        "id": row.id,
        "run_id": row.run_id,
        "step_id": row.step_id,
        "attempt": row.attempt,
        "dispatch_task_id": row.dispatch_task_id,
        "tool_name": row.tool_name,
        "tool_provider": row.tool_provider,
        "tool_schema_digest": row.tool_schema_digest,
        "status": row.status,
        "arguments": _load(row.arguments_redacted_json, {}),
        "result": _load(row.result_projection_json, None),
        "error": _load(row.error_json, None),
        "started_at": row.started_at,
        "deadline_at": row.deadline_at,
        "finished_at": row.finished_at,
    }


def confirmation_payload(row: WorkflowConfirmation) -> Dict[str, Any]:
    return {
        "id": row.id,
        "run_id": row.run_id,
        "step_id": row.step_id,
        "type": row.confirmation_type,
        "status": row.status,
        "risk_summary": row.risk_summary,
        "expires_at": row.expires_at,
        "decision": row.decision,
        "decided_at": row.decided_at,
        "created_at": row.created_at,
    }


def create_run(
    session: Session,
    *,
    user_id: int,
    card_id: str,
    device_id: str,
    input_value: Dict[str, Any],
    version_id: Optional[str] = None,
    idempotency_key: Optional[str] = None,
    actor: RunActorContext = RunActorContext(),
) -> WorkflowRun:
    key = str(idempotency_key or f"auto:{uuid.uuid4().hex}").strip()
    existing = session.exec(
        select(WorkflowRun).where(WorkflowRun.user_id == user_id, WorkflowRun.idempotency_key == key)
    ).first()
    if existing:
        return existing
    card = session.exec(
        select(WorkflowCard).where(
            WorkflowCard.id == card_id,
            WorkflowCard.user_id == user_id,
            WorkflowCard.deleted_at.is_(None),
        )
    ).first()
    if not card:
        raise ValueError("CARD_NOT_FOUND")
    selected_version_id = version_id or card.latest_version_id
    version = session.exec(
        select(WorkflowCardVersion).where(
            WorkflowCardVersion.id == selected_version_id,
            WorkflowCardVersion.card_id == card.id,
        )
    ).first()
    if not version or not WorkflowCard.is_runnable_status(card.status):
        raise ValueError("CARD_VERSION_NOT_RUNNABLE")
    device = session.exec(
        select(DevicePresence).where(
            DevicePresence.user_id == user_id,
            DevicePresence.device_id == device_id,
        )
    ).first()
    if not device:
        raise ValueError("DEVICE_ACCESS_DENIED")
    definition = _load(version.definition_json, {})
    errors = list(Draft202012Validator(definition.get("inputSchema", {"type": "object"})).iter_errors(input_value))
    if errors:
        raise ValueError(f"ARGUMENT_VALIDATION_FAILED: {errors[0].message}")
    now = time.time()
    timeout = int(definition.get("limits", {}).get("timeoutSeconds", 300))
    row = WorkflowRun(
        id=f"wrun_{uuid.uuid4().hex}",
        card_id=card.id,
        card_version_id=version.id,
        user_id=user_id,
        actor_type=actor.actor_type,
        actor_id=actor.actor_id or str(user_id),
        device_id=device_id,
        status="pending",
        current_step_id=str(definition["startStepId"]),
        input_json=encrypt_json(input_value),
        variables_json=_dump({"steps": {}, **(actor.initial_variables or {})}),
        deadline_at=now + timeout,
        next_wakeup_at=now,
        idempotency_key=key,
        created_at=now,
        updated_at=now,
    )
    active_user_runs = session.exec(
        select(WorkflowRun).where(
            WorkflowRun.user_id == user_id,
            WorkflowRun.status.in_(["pending", "running", "waiting_device", "waiting_confirmation", "retry_wait", "paused_offline", "paused"]),
        )
    ).all()
    if len(active_user_runs) >= int(settings.workflow_max_concurrent_per_user):
        raise ValueError("RUN_CONCURRENCY_LIMIT")
    active_device_runs = [item for item in active_user_runs if item.device_id == device_id]
    if len(active_device_runs) >= int(settings.workflow_max_concurrent_per_device):
        raise ValueError("DEVICE_CONCURRENCY_LIMIT")
    session.add(row)
    # WorkflowAuditEvent.run_id has a foreign key but the models do not expose
    # an ORM relationship, so SQLAlchemy cannot infer insert ordering. Persist
    # the parent run before enqueueing its audit row.
    session.flush()
    add_audit(session, event_type="run_created", run=row, status_to="pending")
    session.commit()
    session.refresh(row)
    return row


def fail_run(session: Session, run: WorkflowRun, error: Dict[str, Any], *, status: str = "failed") -> None:
    if run.status in TERMINAL_RUN_STATUSES:
        return
    now = time.time()
    previous = run.status
    run.status = status
    run.error_json = _dump(error)
    run.finished_at = now
    run.next_wakeup_at = None
    run.updated_at = now
    run.lock_version += 1
    session.add(run)
    active_steps = session.exec(
        select(WorkflowStepRun).where(
            WorkflowStepRun.run_id == run.id,
            WorkflowStepRun.status.in_(["dispatch_pending", "dispatching", "waiting_device"]),
        )
    ).all()
    for step in active_steps:
        step.status = "timed_out" if status == "timed_out" else "cancelled"
        step.error_json = step.error_json or _dump(error)
        step.finished_at = now
        step.claim_owner = ""
        step.claimed_at = None
        session.add(step)
    pending_confirmations = session.exec(
        select(WorkflowConfirmation).where(
            WorkflowConfirmation.run_id == run.id,
            WorkflowConfirmation.status == "pending",
        )
    ).all()
    for confirmation in pending_confirmations:
        confirmation.status = "expired" if status == "timed_out" else "cancelled"
        confirmation.decision = confirmation.status
        confirmation.decided_at = now
        session.add(confirmation)
    add_audit(
        session,
        event_type="run_terminal",
        run=run,
        status_from=previous,
        status_to=status,
        detail={"error": _redact(error)},
    )


def advance_run(session: Session, run_id: str) -> Optional[WorkflowRun]:
    run = session.exec(
        select(WorkflowRun).where(WorkflowRun.id == run_id).with_for_update(skip_locked=True)
    ).first()
    if not run or run.status in TERMINAL_RUN_STATUSES or run.status in {"waiting_device", "waiting_confirmation", "paused"}:
        return run
    now = time.time()
    if run.status == "retry_wait" and (run.next_wakeup_at or 0) > now:
        return run
    if now >= run.deadline_at:
        fail_run(session, run, error_payload("RUN_TIMEOUT", "workflow deadline elapsed", "run"), status="timed_out")
        session.commit()
        return run
    version = session.get(WorkflowCardVersion, run.card_version_id)
    if not version:
        fail_run(session, run, error_payload("INTERNAL_STATE_CONFLICT", "workflow version is missing", "run"))
        session.commit()
        return run
    definition = _load(version.definition_json, {})
    limits = definition.get("limits", {})
    if run.transition_count >= int(limits.get("maxTransitions", 100)):
        fail_run(session, run, error_payload("MAX_TRANSITIONS_EXCEEDED", "transition limit reached", "run"))
        session.commit()
        return run
    step = definition.get("steps", {}).get(run.current_step_id)
    if not isinstance(step, dict):
        fail_run(session, run, error_payload("INTERNAL_STATE_CONFLICT", "current step is missing", "advance"))
        session.commit()
        return run
    if run.started_at is None:
        run.started_at = now
    previous_status = run.status
    run.status = "running"
    run.transition_count += 1
    run.lock_version += 1
    entered_step_id = run.current_step_id
    step_type = step.get("type")
    add_audit(
        session,
        event_type="step_entered",
        run=run,
        step_id=entered_step_id,
        status_from=previous_status,
        status_to="running",
        detail={"type": step_type},
    )
    if step_type == "end":
        try:
            output = render_template(step.get("output", definition.get("output", {})), _context(run))
        except Exception as exc:
            fail_run(session, run, error_payload("EXPRESSION_EVALUATION_FAILED", str(exc), "output"))
        else:
            run.status = "succeeded"
            run.output_json = _dump(_redact(output))
            run.finished_at = now
            run.next_wakeup_at = None
            run.updated_at = now
            session.add(run)
            add_audit(
                session,
                event_type="run_succeeded",
                run=run,
                step_id=run.current_step_id,
                status_from="running",
                status_to="succeeded",
            )
        session.commit()
        return run

    if step_type == "condition":
        try:
            matched = evaluate_expression(step.get("expression"), _context(run))
        except Exception as exc:
            fail_run(session, run, error_payload("EXPRESSION_EVALUATION_FAILED", str(exc), "condition"))
        else:
            run.current_step_id = str(step["onTrue"] if matched else step["onFalse"])
            run.status = "running"
            run.next_wakeup_at = now
            run.updated_at = now
            session.add(run)
            add_audit(
                session,
                event_type="condition_evaluated",
                run=run,
                step_id=entered_step_id,
                status_from="running",
                status_to="running",
                detail={"matched": matched, "next": run.current_step_id},
            )
        session.commit()
        return run

    if step_type == "delay":
        delay_seconds = float(step.get("delaySeconds", step.get("seconds", 0)))
        run.current_step_id = str(step["next"])
        run.status = "retry_wait"
        run.next_wakeup_at = min(run.deadline_at, now + delay_seconds)
        run.updated_at = now
        session.add(run)
        add_audit(
            session,
            event_type="delay_started",
            run=run,
            step_id=entered_step_id,
            status_from="running",
            status_to="retry_wait",
            detail={"delay_seconds": delay_seconds, "wake_at": run.next_wakeup_at},
        )
        session.commit()
        return run

    if step_type == "confirm":
        request_confirmation(
            session,
            run=run,
            step_id=run.current_step_id,
            confirmation_type="explicit",
            risk_summary=str(step.get("message") or step.get("riskSummary") or "请确认继续执行自动化卡片"),
            timeout_seconds=int(step.get("timeoutSeconds", 300)),
            next_step_id=str(step["next"]),
            on_denied_step_id=str(step.get("onDenied") or ""),
        )
        session.commit()
        return run

    if step_type != "mcp":
        fail_run(session, run, error_payload("INTERNAL_STATE_CONFLICT", f"unsupported compiled step type: {step_type}", "advance"))
        session.commit()
        return run

    device = session.exec(
        select(DevicePresence).where(
            DevicePresence.user_id == run.user_id,
            DevicePresence.device_id == step_device_id(step, run),
        )
    ).first()
    try:
        arguments = render_template(step.get("arguments", {}), _context(run, device))
    except Exception as exc:
        fail_run(session, run, error_payload("EXPRESSION_EVALUATION_FAILED", str(exc), "arguments"))
        session.commit()
        return run
    timeout = min(int(step.get("timeoutSeconds", 120)), max(1, int(run.deadline_at - now)))
    previous_attempt = session.exec(
        select(WorkflowStepRun)
        .where(WorkflowStepRun.run_id == run.id, WorkflowStepRun.step_id == run.current_step_id)
        .order_by(WorkflowStepRun.attempt.desc())
    ).first()
    attempt = int(previous_attempt.attempt + 1) if previous_attempt else 1
    total_timeout = int(step.get("totalTimeoutSeconds", step.get("timeoutSeconds", 120)))
    first_started_at = now
    if previous_attempt:
        first_attempt = session.exec(
            select(WorkflowStepRun)
            .where(WorkflowStepRun.run_id == run.id, WorkflowStepRun.step_id == run.current_step_id)
            .order_by(WorkflowStepRun.attempt)
        ).first()
        if first_attempt:
            first_started_at = first_attempt.started_at or max(0.0, first_attempt.deadline_at - int(step.get("timeoutSeconds", 120)))
    step_deadline = min(run.deadline_at, now + timeout, first_started_at + total_timeout)
    if step_deadline <= now:
        fail_run(session, run, error_payload("STEP_TIMEOUT", "step retry deadline elapsed", "retry"))
        session.commit()
        return run
    step_run = WorkflowStepRun(
        id=f"wstep_{uuid.uuid4().hex}",
        run_id=run.id,
        step_id=run.current_step_id,
        attempt=attempt,
        dispatch_task_id=f"wftask_{uuid.uuid4().hex}",
        tool_name=str(step["toolRef"]["name"]),
        tool_provider=str(step["toolRef"].get("provider") or ""),
        tool_schema_digest=str(step["toolRef"].get("schemaDigest") or ""),
        status="dispatch_pending",
        arguments_redacted_json=_dump(_redact(arguments)),
        arguments_json="{}",
        deadline_at=step_deadline,
    )
    session.add(step_run)
    run.status = "waiting_device"
    run.next_wakeup_at = now
    run.updated_at = now
    session.add(run)
    add_audit(
        session,
        event_type="step_dispatch_pending",
        run=run,
        step_id=step_run.step_id,
        dispatch_task_id=step_run.dispatch_task_id,
        status_from="running",
        status_to="waiting_device",
        detail={"attempt": attempt, "tool": step_run.tool_name, "arguments": _redact(arguments)},
    )
    session.commit()
    return run


def render_step_arguments(session: Session, step_run: WorkflowStepRun) -> Dict[str, Any]:
    run = session.get(WorkflowRun, step_run.run_id)
    version = session.get(WorkflowCardVersion, run.card_version_id) if run else None
    if not run or not version:
        raise ValueError("workflow run or version is missing")
    definition = _load(version.definition_json, {})
    step = definition["steps"][step_run.step_id]
    device = session.exec(select(DevicePresence).where(DevicePresence.user_id == run.user_id, DevicePresence.device_id == step_device_id(step, run))).first()
    context = _context(run, device)
    rendered = render_template(step.get("arguments", {}), context)
    if not isinstance(rendered, dict):
        raise ValueError("rendered arguments must be an object")
    return resolve_target_arguments(step, context, rendered)


def _handle_step_error(
    session: Session,
    *,
    run: WorkflowRun,
    step_run: WorkflowStepRun,
    step: Dict[str, Any],
    definition: Dict[str, Any],
    error: Dict[str, Any],
) -> None:
    now = time.time()
    step_run.status = "timed_out" if error.get("code") == "STEP_TIMEOUT" else "failed"
    step_run.error_json = _dump(error)
    step_run.finished_at = now
    session.add(step_run)
    retry = step.get("retryPolicy") if isinstance(step.get("retryPolicy"), dict) else {}
    max_attempts = int(retry.get("maxAttempts", 1))
    retry_on = {str(item) for item in retry.get("retryOn", []) if isinstance(item, str)}
    retryable = bool(error.get("retryable")) or str(error.get("code")) in retry_on
    destructive = bool(step_contract(definition, step_run).get("destructive"))
    has_idempotency = bool(retry.get("idempotencyKey") or step.get("idempotencyKey"))
    if retryable and step_run.attempt < max_attempts and (not destructive or has_idempotency):
        base = float(retry.get("delaySeconds", 1))
        delay = base * (2 ** max(0, step_run.attempt - 1)) if retry.get("backoff") == "exponential" else base
        delay = min(delay, float(retry.get("maxDelaySeconds", 60)))
        run.status = "retry_wait"
        run.current_step_id = step_run.step_id
        run.next_wakeup_at = min(run.deadline_at, now + max(0, delay))
        run.updated_at = now
        run.lock_version += 1
        session.add(run)
        add_audit(
            session,
            event_type="step_retry_scheduled",
            run=run,
            step_id=step_run.step_id,
            dispatch_task_id=step_run.dispatch_task_id,
            status_from="waiting_device",
            status_to="retry_wait",
            detail={"attempt": step_run.attempt, "next_attempt": step_run.attempt + 1, "delay_seconds": delay, "error": error},
        )
        return
    on_error = str(step.get("onError") or "fail")
    if on_error not in {"", "fail"}:
        variables = _load(run.variables_json, {"steps": {}})
        variables.setdefault("steps", {})[str(step.get("saveAs"))] = {"error": _redact(error)}
        run.variables_json = _dump(variables)
        run.current_step_id = on_error
        run.status = "running"
        run.next_wakeup_at = now
        run.updated_at = now
        run.lock_version += 1
        session.add(run)
        add_audit(
            session,
            event_type="step_error_branch",
            run=run,
            step_id=step_run.step_id,
            dispatch_task_id=step_run.dispatch_task_id,
            status_from="waiting_device",
            status_to="running",
            detail={"next": on_error, "error": error},
        )
        return
    fail_run(session, run, error, status="timed_out" if error.get("code") == "RUN_TIMEOUT" else "failed")


def apply_step_result(
    session: Session,
    *,
    dispatch_task_id: str,
    success: bool,
    result: Any = None,
    error: Optional[str] = None,
) -> bool:
    step_run = session.exec(
        select(WorkflowStepRun)
        .where(WorkflowStepRun.dispatch_task_id == dispatch_task_id)
        .with_for_update(skip_locked=True)
    ).first()
    if not step_run:
        return False
    if step_run.status in {"succeeded", "failed", "timed_out", "cancelled"}:
        run = session.get(WorkflowRun, step_run.run_id)
        if run:
            add_audit(
                session,
                event_type="step_result_ignored",
                run=run,
                step_id=step_run.step_id,
                dispatch_task_id=dispatch_task_id,
                status_from=run.status,
                status_to=run.status,
                detail={"reason": "step_is_terminal", "step_status": step_run.status},
            )
            session.commit()
        return False
    run = session.exec(
        select(WorkflowRun).where(WorkflowRun.id == step_run.run_id).with_for_update(skip_locked=True)
    ).first()
    if not run or run.status in TERMINAL_RUN_STATUSES:
        step_run.status = "cancelled"
        step_run.finished_at = time.time()
        session.add(step_run)
        if run:
            add_audit(
                session,
                event_type="step_result_ignored",
                run=run,
                step_id=step_run.step_id,
                dispatch_task_id=dispatch_task_id,
                status_from=run.status,
                status_to=run.status,
                detail={"reason": "run_is_terminal"},
            )
        session.commit()
        return False
    now = time.time()
    if now >= min(step_run.deadline_at, run.deadline_at):
        normalized = error_payload("STEP_TIMEOUT", "step deadline elapsed", "device", retryable=True)
        version = session.get(WorkflowCardVersion, run.card_version_id)
        definition = _load(version.definition_json, {}) if version else {}
        definition["_toolContracts"] = _load(version.tool_contracts_json, {}) if version else {}
        step = definition.get("steps", {}).get(step_run.step_id, {})
        _handle_step_error(
            session, run=run, step_run=step_run, step=step, definition=definition, error=normalized
        )
        session.commit()
        return True
    version = session.get(WorkflowCardVersion, run.card_version_id)
    definition = _load(version.definition_json, {}) if version else {}
    definition["_toolContracts"] = _load(version.tool_contracts_json, {}) if version else {}
    step = definition.get("steps", {}).get(step_run.step_id, {})
    normalized = device_step_error(success=success, result=result, transport_error=error)
    if normalized:
        _handle_step_error(
            session, run=run, step_run=step_run, step=step, definition=definition, error=normalized
        )
        session.commit()
        return True
    try:
        projected = _redact(_project_result(result, step.get("resultProjection")))
    except Exception as exc:
        normalized = error_payload("EXPRESSION_EVALUATION_FAILED", str(exc), "result_projection")
        _handle_step_error(
            session, run=run, step_run=step_run, step=step, definition=definition, error=normalized
        )
        session.commit()
        return True
    encoded = _dump(projected)
    variable_result = projected
    if len(encoded.encode("utf-8")) > MAX_PROJECTED_RESULT_BYTES:
        try:
            reference, size = save_result(
                run.user_id,
                run.id,
                projected,
                max_bytes=int(definition.get("limits", {}).get("maxResultBytes", settings.workflow_max_result_bytes)),
            )
        except ValueError:
            normalized = error_payload("STEP_RESULT_TOO_LARGE", "projected result exceeds configured maximum", "result")
            _handle_step_error(
                session, run=run, step_run=step_run, step=step, definition=definition, error=normalized
            )
            session.commit()
            return True
        variable_result = {"$ref": reference, "size": size, "stored": True}
        step_run.result_ref = reference
        encoded = _dump(variable_result)
    variables = _load(run.variables_json, {"steps": {}})
    variables.setdefault("steps", {})[str(step.get("saveAs"))] = {"result": variable_result}
    run.variables_json = _dump(variables)
    run.current_step_id = str(step.get("next") or "")
    run.status = "running"
    run.next_wakeup_at = now
    run.updated_at = now
    run.lock_version += 1
    step_run.status = "succeeded"
    step_run.result_projection_json = encoded
    step_run.finished_at = now
    session.add(step_run)
    session.add(run)
    add_audit(
        session,
        event_type="step_succeeded",
        run=run,
        step_id=step_run.step_id,
        dispatch_task_id=step_run.dispatch_task_id,
        status_from="waiting_device",
        status_to="running",
        detail={"attempt": step_run.attempt, "result": variable_result},
    )
    session.commit()
    return True


def record_ignored_step_result(session: Session, dispatch_task_id: str, *, reason: str) -> bool:
    """Audit duplicate or late device terminal messages without advancing state."""
    step_run = session.exec(
        select(WorkflowStepRun).where(WorkflowStepRun.dispatch_task_id == dispatch_task_id)
    ).first()
    if not step_run:
        return False
    run = session.get(WorkflowRun, step_run.run_id)
    if not run:
        return False
    add_audit(
        session,
        event_type="step_result_ignored",
        run=run,
        step_id=step_run.step_id,
        dispatch_task_id=dispatch_task_id,
        status_from=run.status,
        status_to=run.status,
        detail={"reason": reason, "step_status": step_run.status},
    )
    session.commit()
    return True


def fail_step_dispatch(
    session: Session,
    *,
    dispatch_task_id: str,
    code: str,
    message: str,
    retryable: bool = False,
) -> bool:
    """Fail an un-dispatched step while preserving its specific safety error."""
    step_run = session.exec(
        select(WorkflowStepRun)
        .where(WorkflowStepRun.dispatch_task_id == dispatch_task_id)
        .with_for_update(skip_locked=True)
    ).first()
    if not step_run or step_run.status in {"succeeded", "failed", "timed_out", "cancelled"}:
        return False
    run = session.exec(
        select(WorkflowRun).where(WorkflowRun.id == step_run.run_id).with_for_update(skip_locked=True)
    ).first()
    if not run or run.status in TERMINAL_RUN_STATUSES:
        return False
    error = error_payload(code, message, "dispatch", retryable)
    version = session.get(WorkflowCardVersion, run.card_version_id)
    definition = _load(version.definition_json, {}) if version else {}
    definition["_toolContracts"] = _load(version.tool_contracts_json, {}) if version else {}
    step = definition.get("steps", {}).get(step_run.step_id, {})
    _handle_step_error(
        session, run=run, step_run=step_run, step=step, definition=definition, error=error
    )
    session.commit()
    return True


def cancel_run(session: Session, run: WorkflowRun, reason: str) -> WorkflowRun:
    return cancel_workflow_run(session, run, reason, fail_run)


def wake_offline_runs(session: Session, *, user_id: int, device_id: str) -> int:
    """Actively wake this user's runs when their target device reconnects."""
    now = time.time()
    rows = session.exec(
        select(WorkflowRun)
        .where(
            WorkflowRun.user_id == user_id,
            WorkflowRun.device_id == device_id,
            WorkflowRun.status == "paused_offline",
            WorkflowRun.deadline_at > now,
        )
        .with_for_update(skip_locked=True)
    ).all()
    for run in rows:
        run.status = "waiting_device"
        run.next_wakeup_at = now
        run.updated_at = now
        run.lock_version += 1
        session.add(run)
        add_audit(
            session,
            event_type="device_reconnected",
            run=run,
            status_from="paused_offline",
            status_to="waiting_device",
        )
    if rows:
        session.commit()
    return len(rows)


def retry_failed_run(
    session: Session,
    *,
    run: WorkflowRun,
    user_id: int,
    idempotency_key: Optional[str] = None,
) -> WorkflowRun:
    if run.status not in {"failed", "timed_out", "cancelled"}:
        raise ValueError("RUN_NOT_RETRYABLE")
    return create_run(
        session,
        user_id=user_id,
        card_id=run.card_id,
        device_id=run.device_id,
        input_value=decrypt_json(run.input_json),
        version_id=run.card_version_id,
        idempotency_key=idempotency_key or f"retry:{run.id}:{uuid.uuid4().hex}",
        actor=RunActorContext(actor_type="user", actor_id=str(user_id)),
    )
