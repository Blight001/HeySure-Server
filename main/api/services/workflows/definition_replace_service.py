"""Validated whole-definition replacement with immutable versioning."""

from __future__ import annotations

import json
from typing import Any, Dict

from sqlmodel import Session, select

from api.models import WorkflowCard, WorkflowCardVersion

from .card_service import update_card, version_payload
from .compiler import WorkflowValidationError
from .definition_change_service import change_status, definition_diff, prepare_definition_change
from .schemas import CardUpdate


def _load(raw: str, fallback: Any) -> Any:
    try:
        return json.loads(raw or "")
    except Exception:
        return fallback


def replace_card_definition(
    session: Session,
    *,
    card: WorkflowCard,
    user_id: int,
    base_version_id: str,
    definition: Dict[str, Any],
    dry_run: bool = False,
) -> Dict[str, Any]:
    """Validate a complete definition and optionally save it as the next version."""
    if not dry_run:
        card = session.exec(
            select(WorkflowCard).where(WorkflowCard.id == card.id).with_for_update()
        ).one()
    if not base_version_id or card.latest_version_id != base_version_id:
        raise WorkflowValidationError([
            "card changed since base_version_id; reload before replacing the definition"
        ])
    if not isinstance(definition, dict):
        raise WorkflowValidationError(["replace_definition requires definition to be an object"])
    base = session.get(WorkflowCardVersion, base_version_id)
    if not base or base.card_id != card.id:
        raise WorkflowValidationError(["base card version does not exist"])
    inherited_ids = _load(base.contract_device_ids_json, [])
    if not isinstance(inherited_ids, list):
        inherited_ids = []
    prepared = prepare_definition_change(
        session,
        user_id=user_id,
        definition=definition,
        inherited_device_ids=inherited_ids,
    )
    before = _load(base.definition_json, {})
    diff = definition_diff(
        before, prepared["definition"], before_digest=base.definition_digest,
    )
    result = {
        "card_id": card.id,
        "base_version_id": base_version_id,
        "validation": {
            "valid": True,
            "digest": prepared["digest"],
            "warnings": prepared["warnings"],
        },
        "diff": diff,
        "version": None,
        **change_status(dry_run=dry_run),
    }
    if dry_run:
        return result
    updated = update_card(
        session,
        card,
        CardUpdate(
            definition=definition,
            device_ids=inherited_ids,
            default_device_id=prepared["default_device_id"] or None,
        ),
        user_id=user_id,
    )
    created = session.get(WorkflowCardVersion, updated.latest_version_id)
    result["version"] = version_payload(created, include_definition=True) if created else None
    result.update(change_status(dry_run=False, version_created=created is not None))
    return result
