"""Step-level device binding and nested-card runtime helpers."""

from __future__ import annotations

import json
from typing import Any, Dict, NamedTuple, Optional

from sqlmodel import Session, select

from api.models import WorkflowCard, WorkflowCardVersion, WorkflowRun, WorkflowStepRun


class NestedLimitViolation(NamedTuple):
    frame: Dict[str, Any]
    code: str
    message: str


def _load_list(raw: str) -> list[Any]:
    try:
        value = json.loads(raw or "[]")
    except Exception:
        return []
    return value if isinstance(value, list) else []


def step_device_id(step: Dict[str, Any], run: WorkflowRun) -> str:
    """Use a node binding when present and retain the run device for legacy cards."""
    ref = step.get("toolRef") if isinstance(step.get("toolRef"), dict) else {}
    target = str(ref.get("deviceId") or run.device_id).strip()
    try:
        variables = json.loads(run.variables_json or "{}")
    except Exception:
        variables = {}
    override = variables.get("_device_override") if isinstance(variables, dict) else None
    if isinstance(override, dict) and target == str(override.get("from") or "").strip():
        return str(override.get("to") or target).strip()
    return target


def step_contract(definition: Dict[str, Any], step_run: WorkflowStepRun) -> Dict[str, Any]:
    """Prefer new per-step contracts, falling back to legacy tool-name contracts."""
    contracts = definition.get("_toolContracts", {})
    contract = contracts.get(step_run.step_id) if isinstance(contracts, dict) else None
    if not isinstance(contract, dict) and isinstance(contracts, dict):
        contract = contracts.get(step_run.tool_name)
    return contract if isinstance(contract, dict) else {}


def step_run_device_id(session: Session, step_run: WorkflowStepRun) -> str:
    run = session.get(WorkflowRun, step_run.run_id)
    version = session.get(WorkflowCardVersion, run.card_version_id) if run else None
    try:
        definition = json.loads(version.definition_json or "{}") if version else {}
    except Exception:
        definition = {}
    step = definition.get("steps", {}).get(step_run.step_id, {})
    if not run or not isinstance(step, dict):
        raise ValueError("workflow run step is missing")
    return step_device_id(step, run)


def require_nested_card_access(
    session: Session, *, user_id: int, definition: Dict[str, Any],
    actor_type: str, actor_id: str,
) -> None:
    if actor_type != "ai":
        return
    try:
        ai_config_id = int(actor_id)
    except (TypeError, ValueError) as exc:
        raise ValueError("NESTED_CARD_ACCESS_DENIED") from exc
    card_ids = {
        str(step.get("cardRef", {}).get("id") or "")
        for step in definition.get("steps", {}).values()
        if isinstance(step, dict) and step.get("type") == "_card_enter"
    }
    for card_id in filter(None, card_ids):
        card = session.exec(select(WorkflowCard).where(
            WorkflowCard.id == card_id,
            WorkflowCard.user_id == user_id,
            WorkflowCard.deleted_at.is_(None),
        )).first()
        if not card or not WorkflowCard.accessible_to_ai(
            access_scope=card.access_scope,
            allowed_ai_config_ids=_load_list(card.allowed_ai_config_ids_json),
            tags=_load_list(card.tags_json),
            ai_config_id=ai_config_id,
        ):
            raise ValueError("NESTED_CARD_ACCESS_DENIED")


def nested_frames(variables: Dict[str, Any]) -> list[Dict[str, Any]]:
    frames = variables.get("_nested_cards")
    if not isinstance(frames, list):
        frames = []
        variables["_nested_cards"] = frames
    return frames


def enter_nested_frame(
    variables: Dict[str, Any], *, step_id: str, step: Dict[str, Any],
    run_deadline: float, transition_count: int, now: float,
) -> None:
    limits = step.get("_nestedLimits") if isinstance(step.get("_nestedLimits"), dict) else {}
    nested_frames(variables).append({
        "cardId": str(step.get("cardRef", {}).get("id") or ""),
        "saveAs": str(step.get("saveAs") or step_id),
        "onError": str(step.get("onError") or "fail"),
        "deadlineAt": min(run_deadline, now + int(limits.get("timeoutSeconds", 300))),
        "transitionStart": transition_count,
        "maxTransitions": int(limits.get("maxTransitions", 100)),
    })


def leave_nested_frame(variables: Dict[str, Any]) -> None:
    frames = nested_frames(variables)
    if frames:
        frames.pop()
    if not frames:
        variables.pop("_nested_cards", None)


def nested_limit_violation(
    variables: Dict[str, Any], *, transition_count: int, run_deadline: float, now: float,
) -> Optional[NestedLimitViolation]:
    frames = nested_frames(variables)
    if not frames:
        variables.pop("_nested_cards", None)
        return None
    frame = frames[-1]
    if now >= float(frame.get("deadlineAt") or run_deadline):
        return NestedLimitViolation(frame, "NESTED_CARD_TIMEOUT", "referenced card deadline elapsed")
    used = transition_count - int(frame.get("transitionStart") or 0)
    if used >= int(frame.get("maxTransitions") or 100):
        return NestedLimitViolation(
            frame,
            "NESTED_CARD_MAX_TRANSITIONS_EXCEEDED",
            "referenced card transition limit reached",
        )
    return None
