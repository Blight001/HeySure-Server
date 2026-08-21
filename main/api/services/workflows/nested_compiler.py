"""Expand pinned workflow-card references into one deterministic runtime graph."""

from __future__ import annotations

import hashlib
import re
from copy import deepcopy
from typing import Any, Dict


TEMPLATE_RE = re.compile(r"\$\{([^{}]+)\}")
TARGET_FIELDS = ("next", "onError", "onTrue", "onFalse", "onDenied")


def _safe_id(parent_id: str, child_id: str, kind: str = "step") -> str:
    digest = hashlib.sha256(f"{parent_id}:{child_id}:{kind}".encode()).hexdigest()[:16]
    return f"nested_{digest}"


def _rewrite_string(value: str, input_save_as: str, save_names: Dict[str, str]) -> str:
    def replace(match: re.Match[str]) -> str:
        ref = match.group(1).strip()
        if ref == "input":
            return f"${{steps.{input_save_as}.result.input}}"
        if ref.startswith("input."):
            return f"${{steps.{input_save_as}.result.input.{ref[6:]}}}"
        if ref.startswith("steps."):
            parts = ref.split(".")
            if len(parts) >= 2 and parts[1] in save_names:
                parts[1] = save_names[parts[1]]
                return "${" + ".".join(parts) + "}"
        return match.group(0)

    return TEMPLATE_RE.sub(replace, value)


def _rewrite_value(value: Any, input_save_as: str, save_names: Dict[str, str]) -> Any:
    if isinstance(value, dict):
        return {key: _rewrite_value(child, input_save_as, save_names) for key, child in value.items()}
    if isinstance(value, list):
        return [_rewrite_value(child, input_save_as, save_names) for child in value]
    if isinstance(value, str):
        return _rewrite_string(value, input_save_as, save_names)
    return deepcopy(value)


def _mapped_target(value: Any, step_ids: Dict[str, str], error_id: str) -> Any:
    text = str(value or "")
    if text in {"", "fail"}:
        return error_id
    return step_ids.get(text, text)


def _child_maps(parent_id: str, child_steps: Dict[str, Any]):
    step_ids = {str(key): _safe_id(parent_id, str(key)) for key in child_steps}
    save_names = {
        str(value.get("saveAs")): _safe_id(parent_id, str(value.get("saveAs")), "result")
        for value in child_steps.values()
        if isinstance(value, dict) and value.get("saveAs")
    }
    return step_ids, save_names


def _boundary_steps(
    parent_id: str, step: Dict[str, Any], child: Dict[str, Any], step_ids: Dict[str, str], error_id: str,
) -> Dict[str, Dict[str, Any]]:
    card_ref = step.get("cardRef") if isinstance(step.get("cardRef"), dict) else {}
    input_save_as = str(step.get("saveAs") or parent_id)
    return {
        parent_id: {
            "type": "_card_enter",
            "title": step.get("title") or card_ref.get("name") or "引用卡片",
            "input": deepcopy(step.get("input") if isinstance(step.get("input"), dict) else {}),
            "inputSchema": deepcopy(child.get("inputSchema") or {"type": "object"}),
            "saveAs": input_save_as,
            "next": step_ids.get(str(child.get("startStepId") or ""), ""),
            "onError": str(step.get("onError") or "fail"),
            "cardRef": deepcopy(card_ref),
        },
        error_id: {
            "type": "_card_error",
            "title": f"{card_ref.get('name') or parent_id}失败返回",
            "next": str(step.get("onError") or "fail"),
            "saveAs": input_save_as,
            "_nestedCardId": str(card_ref.get("id") or ""),
        },
    }


def _expanded_child_step(
    raw: Dict[str, Any], *, child: Dict[str, Any], step: Dict[str, Any], input_save_as: str,
    save_names: Dict[str, str], step_ids: Dict[str, str], error_id: str, card_id: str,
) -> Dict[str, Any]:
    nested = _rewrite_value(raw, input_save_as, save_names)
    nested["_nestedCardId"] = card_id
    if nested.get("type") == "end":
        return {
            "type": "_card_return",
            "title": nested.get("title") or "子卡片返回",
            "output": _rewrite_value(raw.get("output", child.get("output", {})), input_save_as, save_names),
            "saveAs": input_save_as,
            "next": str(step.get("next") or ""),
            "_nestedCardId": card_id,
        }
    for field in TARGET_FIELDS:
        if field in nested:
            nested[field] = _mapped_target(nested[field], step_ids, error_id)
    if nested.get("saveAs") in save_names:
        nested["saveAs"] = save_names[str(nested["saveAs"])]
    return nested


def _expand_child(parent_id: str, step: Dict[str, Any]) -> Dict[str, Dict[str, Any]]:
    child = step.get("_definition")
    if not isinstance(child, dict) or not isinstance(child.get("steps"), dict):
        raise ValueError(f"step {parent_id}: referenced card version is missing")
    child_steps = child["steps"]
    step_ids, save_names = _child_maps(parent_id, child_steps)
    error_id = _safe_id(parent_id, "error", "return")
    expanded = _boundary_steps(parent_id, step, child, step_ids, error_id)
    card_ref = step.get("cardRef") if isinstance(step.get("cardRef"), dict) else {}
    input_save_as = str(step.get("saveAs") or parent_id)
    for child_id, raw in child_steps.items():
        expanded[step_ids[str(child_id)]] = _expanded_child_step(
            raw, child=child, step=step, input_save_as=input_save_as, save_names=save_names,
            step_ids=step_ids, error_id=error_id, card_id=str(card_ref.get("id") or ""),
        )
    return expanded


def expand_card_steps(definition: Dict[str, Any]) -> Dict[str, Any]:
    """Return the executable graph while preserving normal definitions unchanged."""
    source = deepcopy(definition)
    steps = source.get("steps")
    if not isinstance(steps, dict) or not any(
        isinstance(step, dict) and step.get("type") == "card" for step in steps.values()
    ):
        return source
    expanded: Dict[str, Dict[str, Any]] = {}
    for step_id, step in steps.items():
        if isinstance(step, dict) and step.get("type") == "card":
            expanded.update(_expand_child(str(step_id), step))
        else:
            expanded[str(step_id)] = deepcopy(step)
    source["steps"] = expanded
    return source
