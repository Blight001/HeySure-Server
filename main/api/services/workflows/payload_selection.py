"""Bounded, read-only projections for workflow card payloads."""

from __future__ import annotations

from copy import deepcopy
from typing import Any, Dict, List, Tuple

from .compiler import WorkflowValidationError


MAX_SELECTED_STEPS = 100


def _string_list(value: Any, path: str, *, allow_empty: bool = True) -> List[str]:
    if not isinstance(value, list) or not all(isinstance(item, str) and item.strip() for item in value):
        raise WorkflowValidationError([f"{path} must be an array of non-empty strings"])
    items = list(dict.fromkeys(item.strip() for item in value))
    if not allow_empty and not items:
        raise WorkflowValidationError([f"{path} must not be empty"])
    if len(items) > MAX_SELECTED_STEPS:
        raise WorkflowValidationError([f"{path} exceeds maximum of {MAX_SELECTED_STEPS}"])
    return items


def _positive_int(value: Any, path: str, *, minimum: int, maximum: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or not minimum <= value <= maximum:
        raise WorkflowValidationError([f"{path} must be {minimum}..{maximum}"])
    return value


def _selected_fields(
    source: Dict[str, Any], requested: Any, path: str,
) -> Tuple[Dict[str, Any], List[str]]:
    if requested is None:
        return deepcopy(source), list(source)
    fields = _string_list(requested, path)
    unknown = [field for field in fields if field not in source]
    if unknown:
        raise WorkflowValidationError([f"{path} contains unknown fields: {', '.join(unknown)}"])
    return {field: deepcopy(source[field]) for field in fields}, fields


def _step_mode(args: Dict[str, Any]) -> Tuple[str, Dict[str, Any]]:
    has_ids = "step_ids" in args
    has_tail = "tail" in args
    has_page = "step_offset" in args or "step_limit" in args
    if sum((has_ids, has_tail, has_page)) > 1:
        raise WorkflowValidationError([
            "step_ids, tail, and step_offset/step_limit are mutually exclusive selection modes"
        ])
    if has_ids:
        return "ids", {"step_ids": _string_list(args.get("step_ids"), "step_ids", allow_empty=False)}
    if has_tail:
        return "tail", {"tail": _positive_int(args.get("tail"), "tail", minimum=1, maximum=MAX_SELECTED_STEPS)}
    if has_page:
        offset = _positive_int(args.get("step_offset", 0), "step_offset", minimum=0, maximum=10_000)
        limit = _positive_int(args.get("step_limit", 20), "step_limit", minimum=1, maximum=MAX_SELECTED_STEPS)
        return "page", {"offset": offset, "limit": limit}
    return "all", {}


def _step_window(
    step_ids: List[str], steps: Dict[str, Any], mode: str, options: Dict[str, Any],
) -> Tuple[List[str], List[str], Any, Any]:
    if mode == "ids":
        requested = options["step_ids"]
        return (
            [step_id for step_id in requested if step_id in steps],
            [step_id for step_id in requested if step_id not in steps],
            None,
            None,
        )
    if mode == "tail":
        limit = options["tail"]
        offset = max(0, len(step_ids) - limit)
        return step_ids[offset:], [], offset, limit
    if mode == "page":
        offset, limit = options["offset"], options["limit"]
        return step_ids[offset:offset + limit], [], offset, limit
    return step_ids, [], 0, len(step_ids)


def _select_steps(
    steps: Dict[str, Any], mode: str, options: Dict[str, Any],
) -> Tuple[Dict[str, Any], Dict[str, Any]]:
    step_ids = list(steps)
    selected_ids, missing, offset, limit = _step_window(step_ids, steps, mode, options)
    returned = {step_id: deepcopy(steps[step_id]) for step_id in selected_ids}
    consumed = len(step_ids) if mode == "ids" else (offset or 0) + len(selected_ids)
    metadata = {
        "mode": mode,
        "total_steps": len(step_ids),
        "returned_steps": len(selected_ids),
        "returned_step_ids": selected_ids,
        "has_more": (
            consumed < len(step_ids) if mode == "page"
            else mode == "tail" and len(selected_ids) < len(step_ids)
        ),
        "next_offset": consumed if mode == "page" and consumed < len(step_ids) else None,
    }
    if missing:
        metadata["missing_step_ids"] = missing
    if offset is not None:
        metadata["offset"] = offset
    if limit is not None:
        metadata["limit"] = limit
    return returned, metadata


def _select_definition(
    definition: Dict[str, Any], requested_fields: Any, mode: str, options: Dict[str, Any], path: str,
) -> Tuple[Dict[str, Any], List[str], Dict[str, Any]]:
    projected, fields = _selected_fields(definition, requested_fields, f"fields.{path}")
    step_selector_used = mode != "all"
    if step_selector_used and "steps" not in projected:
        raise WorkflowValidationError([f"fields.{path} must include steps when a step selector is used"])
    pagination: Dict[str, Any] = {}
    if "steps" in projected:
        steps = definition.get("steps") if isinstance(definition.get("steps"), dict) else {}
        projected["steps"], pagination = _select_steps(steps, mode, options)
    return projected, fields, pagination


def select_card_payload(payload: Dict[str, Any], args: Dict[str, Any]) -> Dict[str, Any]:
    """Return an opt-in bounded view; calls without selectors remain unchanged."""
    selector_keys = {"fields", "step_ids", "step_offset", "step_limit", "tail"}
    if not selector_keys.intersection(args):
        return payload
    fields = args.get("fields", {})
    if not isinstance(fields, dict):
        raise WorkflowValidationError(["fields must be an object"])
    unknown_groups = sorted(set(fields) - {"card", "definition", "version"})
    if unknown_groups:
        raise WorkflowValidationError([f"fields contains unknown groups: {', '.join(unknown_groups)}"])
    mode, options = _step_mode(args)
    card_requested = fields.get("card")
    selected, card_fields = _selected_fields(payload, card_requested, "fields.card")
    if mode != "all" and "definition" not in selected and "version" not in selected:
        raise WorkflowValidationError(["fields.card must include definition or version when a step selector is used"])

    selected_definition_fields: List[str] = []
    selected_version_fields: List[str] = []
    selected_version_definition_fields: List[str] = []
    pagination: Dict[str, Any] = {}
    if isinstance(selected.get("definition"), dict):
        selected["definition"], selected_definition_fields, pagination["definition"] = _select_definition(
            selected["definition"], fields.get("definition"), mode, options, "definition",
        )
    if isinstance(selected.get("version"), dict):
        selected["version"], selected_version_fields = _selected_fields(
            selected["version"], fields.get("version"), "fields.version",
        )
        if isinstance(selected["version"].get("definition"), dict):
            version_definition = selected["version"]["definition"]
            (
                selected["version"]["definition"],
                selected_version_definition_fields,
                pagination["version"],
            ) = _select_definition(
                version_definition, fields.get("definition"), mode, options, "definition",
            )
    if mode != "all" and not pagination:
        raise WorkflowValidationError([
            "step selection requires a returned definition.steps or version.definition.steps field"
        ])

    selected["selection"] = {
        "card_fields": card_fields,
        "definition_fields": selected_definition_fields,
        "version_fields": selected_version_fields,
        "version_definition_fields": selected_version_definition_fields,
        "step_mode": mode,
        **options,
    }
    selected["pagination"] = pagination
    return selected
