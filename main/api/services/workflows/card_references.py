"""Resolve workflow-card nodes to immutable, user-owned versions at publish time."""

from __future__ import annotations

import json
from copy import deepcopy
from typing import Any, Dict, Tuple

from sqlmodel import Session, select

from api.models import WorkflowCard, WorkflowCardVersion

from .compiler import WorkflowValidationError


def _load(raw: str) -> Dict[str, Any]:
    try:
        value = json.loads(raw or "{}")
    except Exception:
        value = {}
    return value if isinstance(value, dict) else {}


def _referenced_version(
    session: Session, *, user_id: int, card_id: str, version_id: str,
) -> Tuple[WorkflowCard | None, WorkflowCardVersion | None]:
    card = session.exec(select(WorkflowCard).where(
        WorkflowCard.id == card_id,
        WorkflowCard.user_id == user_id,
        WorkflowCard.deleted_at.is_(None),
    )).first()
    if not card:
        return None, None
    selected_version_id = version_id or str(card.latest_version_id or "")
    version = session.exec(select(WorkflowCardVersion).where(
        WorkflowCardVersion.id == selected_version_id,
        WorkflowCardVersion.card_id == card.id,
    )).first()
    return card, version


def resolve_card_references(
    session: Session, *, user_id: int, parent_card_id: str, definition: Dict[str, Any],
) -> Tuple[Dict[str, Any], Dict[str, Any]]:
    """Return (hydrated executable source, pinned editor source)."""
    hydrated = deepcopy(definition)
    pinned = deepcopy(definition)
    hydrated_steps = hydrated.get("steps") if isinstance(hydrated.get("steps"), dict) else {}
    pinned_steps = pinned.get("steps") if isinstance(pinned.get("steps"), dict) else {}
    errors = []
    for step_id, step in hydrated_steps.items():
        if not isinstance(step, dict) or step.get("type") != "card":
            continue
        ref = step.get("cardRef") if isinstance(step.get("cardRef"), dict) else {}
        card_id = str(ref.get("id") or "").strip()
        if not card_id:
            errors.append(f"step {step_id}: referenced card is required")
            continue
        if card_id == parent_card_id:
            errors.append(f"step {step_id}: a card cannot reference itself")
            continue
        card, version = _referenced_version(
            session,
            user_id=user_id,
            card_id=card_id,
            version_id=str(ref.get("versionId") or ""),
        )
        if not card:
            errors.append(f"step {step_id}: referenced card does not exist")
            continue
        if not version:
            errors.append(f"step {step_id}: referenced card version does not exist")
            continue
        normalized_ref = {"id": card.id, "versionId": version.id, "name": card.name}
        step["cardRef"] = normalized_ref
        step["_definition"] = _load(version.definition_json)
        if isinstance(pinned_steps.get(step_id), dict):
            pinned_steps[step_id]["cardRef"] = normalized_ref
            pinned_steps[step_id].pop("_definition", None)
    if errors:
        raise WorkflowValidationError(errors)
    return hydrated, pinned
