"""Optimistic, path-scoped updates for immutable workflow-card versions."""

from __future__ import annotations

from copy import deepcopy
import json
from typing import Any, Dict, Iterable, List

from sqlmodel import Session, select

from api.models import WorkflowCard, WorkflowCardVersion

from .card_service import update_card, version_payload
from .compiler import WorkflowValidationError
from .definition_change_service import change_status, definition_diff, prepare_definition_change
from .schemas import CardUpdate


PATCHABLE_ROOTS = {
    "name", "description", "inputSchema", "startStepId", "steps", "limits",
    "output", "requiredCapabilities", "compatibility",
}


def _card_for_change(session: Session, card: WorkflowCard, *, dry_run: bool) -> WorkflowCard:
    if dry_run:
        return card
    return session.exec(
        select(WorkflowCard).where(WorkflowCard.id == card.id).with_for_update()
    ).one()


def _load(raw: str) -> Dict[str, Any]:
    try:
        value = json.loads(raw or "{}")
    except Exception:
        value = {}
    return value if isinstance(value, dict) else {}


def _segments(path: object) -> List[str]:
    value = str(path or "")
    if not value.startswith("/"):
        raise WorkflowValidationError(["patch path must be an absolute JSON pointer"])
    parts = [item.replace("~1", "/").replace("~0", "~") for item in value[1:].split("/")]
    if not parts or parts[0] not in PATCHABLE_ROOTS or any(part in {"__proto__", "constructor", "prototype"} for part in parts):
        raise WorkflowValidationError([f"patch path is not allowed: {value}"])
    return parts


def _parent(document: Any, parts: List[str]) -> tuple[Any, str]:
    current = document
    for part in parts[:-1]:
        if isinstance(current, list):
            try:
                current = current[int(part)]
            except (ValueError, IndexError):
                raise WorkflowValidationError([f"patch path does not exist: /{'/'.join(parts)}"])
        elif isinstance(current, dict) and part in current:
            current = current[part]
        else:
            raise WorkflowValidationError([f"patch path does not exist: /{'/'.join(parts)}"])
    return current, parts[-1]


def _read(document: Any, parts: List[str]) -> Any:
    parent, key = _parent(document, parts)
    try:
        return parent[int(key)] if isinstance(parent, list) else parent[key]
    except (KeyError, IndexError, ValueError, TypeError):
        raise WorkflowValidationError([f"patch path does not exist: /{'/'.join(parts)}"])


def _write(document: Any, parts: List[str], value: Any, *, replace: bool) -> None:
    parent, key = _parent(document, parts)
    if isinstance(parent, list):
        if key == "-" and not replace:
            parent.append(deepcopy(value))
            return
        try:
            index = int(key)
        except ValueError:
            raise WorkflowValidationError(["array patch index must be an integer or '-'"])
        if replace:
            if index < 0 or index >= len(parent):
                raise WorkflowValidationError(["replace target does not exist"])
            parent[index] = deepcopy(value)
        else:
            if index < 0 or index > len(parent):
                raise WorkflowValidationError(["add target index is out of range"])
            parent.insert(index, deepcopy(value))
        return
    if not isinstance(parent, dict):
        raise WorkflowValidationError(["patch target parent is not an object"])
    if replace and key not in parent:
        raise WorkflowValidationError(["replace target does not exist"])
    parent[key] = deepcopy(value)


def _apply(document: Dict[str, Any], operation: Dict[str, Any]) -> str:
    action = str(operation.get("op") or "").strip().lower()
    parts = _segments(operation.get("path"))
    if action == "test":
        if _read(document, parts) != operation.get("value"):
            raise WorkflowValidationError([f"patch test failed: {operation.get('path')}"])
    elif action in {"add", "replace"}:
        if "value" not in operation:
            raise WorkflowValidationError([f"patch {action} requires value"])
        _write(document, parts, operation["value"], replace=action == "replace")
    elif action == "remove":
        parent, key = _parent(document, parts)
        try:
            parent.pop(int(key)) if isinstance(parent, list) else parent.pop(key)
        except (KeyError, IndexError, ValueError, TypeError):
            raise WorkflowValidationError([f"remove target does not exist: {operation.get('path')}"])
    else:
        raise WorkflowValidationError(["patch op must be add, replace, remove, or test"])
    return str(operation.get("path"))


def patch_card_definition(
    session: Session,
    *,
    card: WorkflowCard,
    user_id: int,
    base_version_id: str,
    operations: Iterable[Dict[str, Any]],
    dry_run: bool = False,
) -> Dict[str, Any]:
    ops = list(operations)
    card = _card_for_change(session, card, dry_run=dry_run)
    if not base_version_id or card.latest_version_id != base_version_id:
        raise WorkflowValidationError(["card changed since base_version_id; reload before applying a patch"])
    if not 1 <= len(ops) <= 100:
        raise WorkflowValidationError(["patch requires 1..100 operations"])
    version = session.get(WorkflowCardVersion, base_version_id)
    if not version or version.card_id != card.id:
        raise WorkflowValidationError(["base card version does not exist"])
    definition = _load(version.definition_json)
    changed_paths = [_apply(definition, item) for item in ops if isinstance(item, dict)]
    if len(changed_paths) != len(ops):
        raise WorkflowValidationError(["every patch operation must be an object"])
    try:
        device_ids = json.loads(version.contract_device_ids_json or "[]")
    except Exception:
        device_ids = []
    if not isinstance(device_ids, list):
        device_ids = []
    prepared = prepare_definition_change(
        session, user_id=user_id, definition=definition, inherited_device_ids=device_ids,
    )
    diff = definition_diff(
        _load(version.definition_json), prepared["definition"],
        before_digest=version.definition_digest,
    )
    result = {
        "card_id": card.id,
        "base_version_id": base_version_id,
        "version": None,
        "changed_paths": changed_paths,
        "validation": {
            "valid": True,
            "digest": prepared["digest"],
            "warnings": prepared["warnings"],
        },
        "diff": diff,
        **change_status(dry_run=dry_run),
    }
    if dry_run:
        return result
    updated = update_card(
        session,
        card,
        CardUpdate(
            definition=definition,
            device_ids=device_ids,
            default_device_id=str(definition.get("defaultDeviceId") or "") or None,
        ),
        user_id=user_id,
    )
    created = session.get(WorkflowCardVersion, updated.latest_version_id)
    result["version"] = version_payload(created, include_definition=True) if created else None
    result.update(change_status(dry_run=False, version_created=created is not None))
    return result
