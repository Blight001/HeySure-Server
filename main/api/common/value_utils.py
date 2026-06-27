"""Lenient value-coercion helpers shared across the 4 server processes.

Single source of truth for the "tolerant parse" patterns that were previously
copy-pasted (``json.loads(... or "[]")`` wrapped in try/except, ad-hoc bool
coercion of form/query/JSON values). Keeping them here avoids subtle drift
between the gateway, mcp_runtime, connector_runtime and ai_runtime copies.
"""

from __future__ import annotations

import json
from typing import Any, Dict, List

_TRUE_TOKENS = {"1", "true", "yes", "on"}
_FALSE_TOKENS = {"0", "false", "no", "off"}


def to_bool(value: Any, default: bool = False) -> bool:
    """Coerce loose input (bool / number / string flag) to a bool.

    Strings match the usual ``1/true/yes/on`` and ``0/false/no/off`` tokens
    (case-insensitive); anything unrecognised falls back to ``default``.
    """
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    text = str(value or "").strip().lower()
    if text in _TRUE_TOKENS:
        return True
    if text in _FALSE_TOKENS:
        return False
    return default


def safe_json(raw: Any, default: Any = None) -> Any:
    """Parse a JSON string, returning ``default`` on empty/invalid input."""
    if not raw:
        return default
    try:
        return json.loads(raw)
    except Exception:
        return default


def safe_json_obj(raw: Any) -> Dict[str, Any]:
    """Parse a JSON string expected to hold an object; ``{}`` on anything else."""
    parsed = safe_json(raw, {})
    return parsed if isinstance(parsed, dict) else {}


def safe_json_list(raw: Any) -> List[Any]:
    """Parse a JSON string expected to hold an array; ``[]`` on anything else."""
    parsed = safe_json(raw, [])
    return parsed if isinstance(parsed, list) else []
