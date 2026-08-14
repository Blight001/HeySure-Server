"""Validated whole-definition replacement with immutable versioning."""

from __future__ import annotations

from copy import deepcopy
import json
from typing import Any, Dict, List

from sqlmodel import Session

from api.models import WorkflowCard, WorkflowCardVersion

from .card_service import _snapshot_contracts, update_card, version_payload
from .compiler import WorkflowValidationError, compile_definition, definition_digest
from .schemas import CardUpdate


MAX_DIFF_PATHS = 1000


def _load(raw: str, fallback: Any) -> Any:
    try:
        return json.loads(raw or "")
    except Exception:
        return fallback


def _pointer(path: str, key: object) -> str:
    escaped = str(key).replace("~", "~0").replace("/", "~1")
    return f"{path}/{escaped}"


def _diff_paths(before: Any, after: Any) -> Dict[str, Any]:
    added: List[str] = []
    removed: List[str] = []
    changed: List[str] = []
    total = 0

    def append(target: List[str], path: str) -> None:
        nonlocal total
        total += 1
        if sum(map(len, (added, removed, changed))) < MAX_DIFF_PATHS:
            target.append(path or "/")

    def walk(left: Any, right: Any, path: str = "") -> None:
        if isinstance(left, dict) and isinstance(right, dict):
            for key in sorted(left.keys() - right.keys()):
                append(removed, _pointer(path, key))
            for key in sorted(right.keys() - left.keys()):
                append(added, _pointer(path, key))
            for key in sorted(left.keys() & right.keys()):
                walk(left[key], right[key], _pointer(path, key))
            return
        if isinstance(left, list) and isinstance(right, list):
            if left != right:
                append(changed, path)
            return
        if left != right:
            append(changed, path)

    walk(before, after)
    return {
        "added_paths": added,
        "removed_paths": removed,
        "changed_paths": changed,
        "change_count": total,
        "truncated": total > MAX_DIFF_PATHS,
    }


def _prepare_definition(
    session: Session,
    *,
    user_id: int,
    definition: Dict[str, Any],
    inherited_device_ids: List[str],
) -> Dict[str, Any]:
    candidate = deepcopy(definition)
    candidate.setdefault("schemaVersion", 1)
    compiled = compile_definition(candidate)
    normalized = compiled["definition"]
    contracts, bound_ids = _snapshot_contracts(
        session, user_id, normalized, device_ids=inherited_device_ids,
    )
    selected_default = str(normalized.get("defaultDeviceId") or "").strip()
    if not selected_default and bound_ids:
        selected_default = bound_ids[0]
    if selected_default and selected_default not in bound_ids:
        raise WorkflowValidationError(["default device must be one of the selected contract device IDs"])
    if selected_default:
        normalized["defaultDeviceId"] = selected_default
    normalized["contractDeviceIds"] = bound_ids
    return {
        "definition": normalized,
        "contracts": contracts,
        "device_ids": bound_ids,
        "default_device_id": selected_default,
        "digest": definition_digest(normalized),
        "warnings": compiled["warnings"],
    }


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
    prepared = _prepare_definition(
        session,
        user_id=user_id,
        definition=definition,
        inherited_device_ids=inherited_ids,
    )
    before = _load(base.definition_json, {})
    diff = _diff_paths(before, prepared["definition"])
    diff.update({
        "before_digest": base.definition_digest or definition_digest(before),
        "after_digest": prepared["digest"],
    })
    result = {
        "card_id": card.id,
        "base_version_id": base_version_id,
        "dry_run": bool(dry_run),
        "validation": {
            "valid": True,
            "digest": prepared["digest"],
            "warnings": prepared["warnings"],
        },
        "diff": diff,
        "version": None,
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
    return result
