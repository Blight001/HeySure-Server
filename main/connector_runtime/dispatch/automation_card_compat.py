"""Compatibility rewrites for endpoint-native automation card payloads."""

from __future__ import annotations

import copy
import re
from typing import Any, Dict


_AIFREE_MCP_MARKER = "__aifree_"
_CARD_WRITE_ACTIONS = {"write", "patch_step", "insert_step"}


def local_aifree_tool_name(value: object) -> str:
    """Return the local AI-FREE router name for one advertised alias."""
    raw = str(value or "").strip()
    lowered = raw.lower()
    if lowered.startswith("mcp__"):
        marker_index = lowered.rfind(_AIFREE_MCP_MARKER)
        if marker_index < 0:
            return raw
        raw = raw[marker_index + len(_AIFREE_MCP_MARKER) :]
    elif lowered.startswith("aifree."):
        raw = raw[len("aifree.") :]
    elif lowered.startswith("aifree_"):
        raw = raw[len("aifree_") :]
    else:
        return raw
    return re.sub(r"[-+.]+", "_", raw)


def _is_aifree_card_tool(value: object) -> bool:
    raw = str(value or "").strip()
    if raw == "manage_card":
        return True
    lowered = raw.lower()
    if not lowered.startswith(("mcp__", "aifree.", "aifree_")):
        return False
    return local_aifree_tool_name(raw) == "manage_card"


def _normalize_step(step: Any) -> None:
    if not isinstance(step, dict) or str(step.get("type") or "").strip().lower() != "mcp":
        return
    tool = step.get("tool")
    normalized = local_aifree_tool_name(tool)
    if normalized and normalized != tool:
        step["tool"] = normalized


def normalize_automation_card_arguments(tool: object, arguments: Any) -> Dict[str, Any]:
    """Rewrite namespaced inner MCP names before old AI-FREE clients persist them."""
    if not isinstance(arguments, dict):
        return {}
    action = str(arguments.get("action") or "").strip().lower()
    if not _is_aifree_card_tool(tool) or action not in _CARD_WRITE_ACTIONS:
        return arguments

    normalized = copy.deepcopy(arguments)
    card_data = normalized.get("cardData")
    if isinstance(card_data, dict) and isinstance(card_data.get("steps"), list):
        for step in card_data["steps"]:
            _normalize_step(step)
    for key in ("stepData", "stepPatch"):
        _normalize_step(normalized.get(key))
    return normalized
