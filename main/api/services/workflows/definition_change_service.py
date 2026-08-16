"""Shared validation and readable diffs for workflow definition changes."""

from __future__ import annotations

from copy import deepcopy
from typing import Any, Dict, List

from sqlmodel import Session

from .card_service import _snapshot_contracts
from .compiler import WorkflowValidationError, compile_definition, definition_digest


MAX_DIFF_PATHS = 1000


def _pointer(path: str, key: object) -> str:
    escaped = str(key).replace("~", "~0").replace("/", "~1")
    return f"{path}/{escaped}"


def _collect_diff(before: Any, after: Any) -> Dict[str, Any]:
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


def _step_field(path: str, step_id: str) -> str:
    prefix = f"/steps/{step_id}/"
    return path[len(prefix):].replace("/", ".") if path.startswith(prefix) else ""


def _step_added(step_id: str, step: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "step_id": step_id, "change": "added", "type": str(step.get("type") or ""),
        "summary": f"新增步骤 {step_id}（{step.get('type') or 'unknown'}）",
    }


def _step_removed(step_id: str, step: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "step_id": step_id, "change": "removed", "type": str(step.get("type") or ""),
        "summary": f"删除步骤 {step_id}（{step.get('type') or 'unknown'}）",
    }


def _step_modified(
    step_id: str, before: Dict[str, Any], after: Dict[str, Any], paths: Dict[str, Any],
) -> Dict[str, Any] | None:
    prefix = f"/steps/{step_id}"
    relevant = [
        path for group in ("added_paths", "removed_paths", "changed_paths")
        for path in paths[group] if path.startswith(prefix + "/") or path == prefix
    ]
    if not relevant:
        return None
    fields = sorted({field for path in relevant if (field := _step_field(path, step_id))})
    summaries = [f"{field} 已变更" for field in fields]
    old_next, new_next = before.get("next"), after.get("next")
    if old_next != new_next:
        summaries = [item for item in summaries if item != "next 已变更"]
        summaries.insert(0, f"next: {old_next or '无'} → {new_next or '无'}")
    return {
        "step_id": step_id, "change": "modified", "type": str(after.get("type") or ""),
        "fields": fields, "summary": "；".join(summaries),
    }


def _one_step_change(
    step_id: str, old_steps: Dict[str, Any], new_steps: Dict[str, Any], paths: Dict[str, Any],
) -> Dict[str, Any] | None:
    if step_id not in old_steps:
        return _step_added(step_id, new_steps[step_id])
    if step_id not in new_steps:
        return _step_removed(step_id, old_steps[step_id])
    return _step_modified(step_id, old_steps[step_id], new_steps[step_id], paths)


def _step_changes(before: Dict[str, Any], after: Dict[str, Any], paths: Dict[str, Any]) -> List[Dict[str, Any]]:
    old_steps = before.get("steps") if isinstance(before.get("steps"), dict) else {}
    new_steps = after.get("steps") if isinstance(after.get("steps"), dict) else {}
    changes = [
        _one_step_change(step_id, old_steps, new_steps, paths)
        for step_id in sorted(old_steps.keys() | new_steps.keys())
    ]
    return [item for item in changes if item is not None]


def definition_diff(before: Dict[str, Any], after: Dict[str, Any], *, before_digest: str = "") -> Dict[str, Any]:
    paths = _collect_diff(before, after)
    paths["step_changes"] = _step_changes(before, after, paths)
    paths["before_digest"] = before_digest or definition_digest(before)
    paths["after_digest"] = definition_digest(after)
    return paths


def prepare_definition_change(
    session: Session,
    *,
    user_id: int,
    definition: Dict[str, Any],
    inherited_device_ids: List[str],
) -> Dict[str, Any]:
    if not isinstance(definition, dict):
        raise WorkflowValidationError(["definition must be an object"])
    candidate = deepcopy(definition)
    candidate.setdefault("schemaVersion", 1)
    compiled = compile_definition(candidate)
    normalized = compiled["definition"]
    # A definition change is validated against the live contract snapshot. Frozen
    # digests are publication artifacts, not caller input; retaining an expired
    # digest on an untouched MCP step must not block an otherwise unrelated patch.
    for step in normalized.get("steps", {}).values():
        if isinstance(step, dict) and step.get("type") == "mcp":
            ref = step.get("toolRef") if isinstance(step.get("toolRef"), dict) else {}
            ref.pop("schemaDigest", None)
            ref.pop("provider", None)
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


def change_status(*, dry_run: bool, version_created: bool = False) -> Dict[str, Any]:
    return {
        "dry_run": bool(dry_run),
        "operation_status": "validated" if dry_run else "applied",
        "applied": not dry_run,
        "committed": not dry_run,
        "version_created": bool(version_created),
    }
