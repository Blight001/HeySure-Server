"""Transactional state transitions for deterministic workflow runs."""

from __future__ import annotations

import json
import time
import uuid
from typing import Any, Dict, Optional

from jsonschema import Draft202012Validator
from sqlmodel import Session, select

from api.models import (
    DevicePresence,
    WorkflowCard,
    WorkflowCardVersion,
    WorkflowRun,
    WorkflowStepRun,
)

from .expression import render_template


TERMINAL_RUN_STATUSES = {"succeeded", "failed", "cancelled", "timed_out"}
SENSITIVE_KEYS = {"authorization", "cookie", "password", "secret", "token", "api_key", "apikey"}
MAX_PROJECTED_RESULT_BYTES = 64 * 1024


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
        "input": _load(run.input_json, {}),
        "steps": variables.get("steps", {}),
        "run": {"id": run.id, "startedAt": run.started_at, "createdAt": run.created_at},
        "device": {
            "id": run.device_id,
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
    }


def step_payload(row: WorkflowStepRun) -> Dict[str, Any]:
    return {
        "id": row.id,
        "run_id": row.run_id,
        "step_id": row.step_id,
        "attempt": row.attempt,
        "dispatch_task_id": row.dispatch_task_id,
        "tool_name": row.tool_name,
        "tool_schema_digest": row.tool_schema_digest,
        "status": row.status,
        "arguments": _load(row.arguments_redacted_json, {}),
        "result": _load(row.result_projection_json, None),
        "error": _load(row.error_json, None),
        "started_at": row.started_at,
        "deadline_at": row.deadline_at,
        "finished_at": row.finished_at,
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
    actor_type: str = "user",
    actor_id: str = "",
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
    if not version or card.status not in {"published", "deprecated"}:
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
        actor_type=actor_type,
        actor_id=actor_id or str(user_id),
        device_id=device_id,
        status="pending",
        current_step_id=str(definition["startStepId"]),
        input_json=_dump(input_value),
        variables_json=_dump({"steps": {}}),
        deadline_at=now + timeout,
        next_wakeup_at=now,
        idempotency_key=key,
        created_at=now,
        updated_at=now,
    )
    session.add(row)
    session.commit()
    session.refresh(row)
    return row


def fail_run(session: Session, run: WorkflowRun, error: Dict[str, Any], *, status: str = "failed") -> None:
    if run.status in TERMINAL_RUN_STATUSES:
        return
    now = time.time()
    run.status = status
    run.error_json = _dump(error)
    run.finished_at = now
    run.next_wakeup_at = None
    run.updated_at = now
    run.lock_version += 1
    session.add(run)


def advance_run(session: Session, run_id: str) -> Optional[WorkflowRun]:
    run = session.exec(
        select(WorkflowRun).where(WorkflowRun.id == run_id).with_for_update(skip_locked=True)
    ).first()
    if not run or run.status in TERMINAL_RUN_STATUSES or run.status == "waiting_device":
        return run
    now = time.time()
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
    run.status = "running"
    run.transition_count += 1
    run.lock_version += 1
    if step.get("type") == "end":
        try:
            output = render_template(definition.get("output", {}), _context(run))
        except Exception as exc:
            fail_run(session, run, error_payload("EXPRESSION_EVALUATION_FAILED", str(exc), "output"))
        else:
            run.status = "succeeded"
            run.output_json = _dump(_redact(output))
            run.finished_at = now
            run.next_wakeup_at = None
            run.updated_at = now
            session.add(run)
        session.commit()
        return run

    device = session.exec(
        select(DevicePresence).where(
            DevicePresence.user_id == run.user_id,
            DevicePresence.device_id == run.device_id,
        )
    ).first()
    try:
        arguments = render_template(step.get("arguments", {}), _context(run, device))
    except Exception as exc:
        fail_run(session, run, error_payload("EXPRESSION_EVALUATION_FAILED", str(exc), "arguments"))
        session.commit()
        return run
    timeout = min(int(step.get("timeoutSeconds", 120)), max(1, int(run.deadline_at - now)))
    step_run = WorkflowStepRun(
        id=f"wstep_{uuid.uuid4().hex}",
        run_id=run.id,
        step_id=run.current_step_id,
        attempt=1,
        dispatch_task_id=f"wftask_{uuid.uuid4().hex}",
        tool_name=str(step["toolRef"]["name"]),
        tool_schema_digest=str(step["toolRef"].get("schemaDigest") or ""),
        status="dispatch_pending",
        arguments_redacted_json=_dump(_redact(arguments)),
        arguments_json="{}",
        deadline_at=now + timeout,
    )
    session.add(step_run)
    run.status = "waiting_device"
    run.next_wakeup_at = now
    run.updated_at = now
    session.add(run)
    session.commit()
    return run


def render_step_arguments(session: Session, step_run: WorkflowStepRun) -> Dict[str, Any]:
    run = session.get(WorkflowRun, step_run.run_id)
    version = session.get(WorkflowCardVersion, run.card_version_id) if run else None
    if not run or not version:
        raise ValueError("workflow run or version is missing")
    definition = _load(version.definition_json, {})
    step = definition["steps"][step_run.step_id]
    device = session.exec(select(DevicePresence).where(DevicePresence.device_id == run.device_id)).first()
    rendered = render_template(step.get("arguments", {}), _context(run, device))
    if not isinstance(rendered, dict):
        raise ValueError("rendered arguments must be an object")
    return rendered


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
    if not step_run or step_run.status in {"succeeded", "failed", "timed_out", "cancelled"}:
        return False
    run = session.exec(
        select(WorkflowRun).where(WorkflowRun.id == step_run.run_id).with_for_update(skip_locked=True)
    ).first()
    if not run or run.status in TERMINAL_RUN_STATUSES:
        step_run.status = "cancelled"
        step_run.finished_at = time.time()
        session.add(step_run)
        session.commit()
        return False
    now = time.time()
    if now >= min(step_run.deadline_at, run.deadline_at):
        normalized = error_payload("STEP_TIMEOUT", "step deadline elapsed", "device")
        step_run.status = "timed_out"
        step_run.error_json = _dump(normalized)
        step_run.finished_at = now
        fail_run(session, run, normalized, status="timed_out")
        session.add(step_run)
        session.commit()
        return True
    version = session.get(WorkflowCardVersion, run.card_version_id)
    definition = _load(version.definition_json, {}) if version else {}
    step = definition.get("steps", {}).get(step_run.step_id, {})
    if not success:
        normalized = error_payload("DISPATCH_FAILED", str(error or "device tool failed"), "device")
        step_run.status = "failed"
        step_run.error_json = _dump(normalized)
        step_run.finished_at = now
        fail_run(session, run, normalized)
        session.add(step_run)
        session.commit()
        return True
    try:
        projected = _redact(_project_result(result, step.get("resultProjection")))
    except Exception as exc:
        normalized = error_payload("EXPRESSION_EVALUATION_FAILED", str(exc), "result_projection")
        step_run.status = "failed"
        step_run.error_json = _dump(normalized)
        step_run.finished_at = now
        fail_run(session, run, normalized)
        session.add(step_run)
        session.commit()
        return True
    encoded = _dump(projected)
    if len(encoded.encode("utf-8")) > MAX_PROJECTED_RESULT_BYTES:
        normalized = error_payload("STEP_RESULT_TOO_LARGE", "projected result exceeds 64 KiB", "result")
        step_run.status = "failed"
        step_run.error_json = _dump(normalized)
        step_run.finished_at = now
        fail_run(session, run, normalized)
        session.add(step_run)
        session.commit()
        return True
    variables = _load(run.variables_json, {"steps": {}})
    variables.setdefault("steps", {})[str(step.get("saveAs"))] = {"result": projected}
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
    now = time.time()
    step_run.status = "failed"
    step_run.error_json = _dump(error)
    step_run.finished_at = now
    fail_run(session, run, error, status="timed_out" if code in {"STEP_TIMEOUT", "RUN_TIMEOUT"} else "failed")
    session.add(step_run)
    session.commit()
    return True


def cancel_run(session: Session, run: WorkflowRun, reason: str) -> WorkflowRun:
    if run.status not in TERMINAL_RUN_STATUSES:
        fail_run(session, run, error_payload("RUN_CANCELLED", reason, "cancel"), status="cancelled")
        steps = session.exec(
            select(WorkflowStepRun).where(
                WorkflowStepRun.run_id == run.id,
                WorkflowStepRun.status.in_(["dispatch_pending", "waiting_device"]),
            )
        ).all()
        for step in steps:
            step.status = "cancelled"
            step.finished_at = time.time()
            session.add(step)
        session.commit()
        session.refresh(run)
    return run
