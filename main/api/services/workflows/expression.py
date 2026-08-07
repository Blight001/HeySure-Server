"""Safe variable-path and string-template resolution (no eval/Jinja)."""

from __future__ import annotations

import re
from typing import Any, Dict


TEMPLATE_RE = re.compile(r"\$\{([^{}]+)\}")
SAFE_ROOTS = {"input", "steps", "run", "device"}


class TemplateResolutionError(ValueError):
    pass


def resolve_path(path: str, context: Dict[str, Any]) -> Any:
    parts = [item.strip() for item in str(path).split(".")]
    if not parts or parts[0] not in SAFE_ROOTS or len(parts) > 16:
        raise TemplateResolutionError(f"forbidden variable path: {path}")
    current: Any = context
    for part in parts:
        if not part or part.startswith("__") or not isinstance(current, dict) or part not in current:
            raise TemplateResolutionError(f"unavailable variable path: {path}")
        current = current[part]
    return current


def render_template(value: Any, context: Dict[str, Any], depth: int = 0) -> Any:
    if depth > 16:
        raise TemplateResolutionError("template nesting exceeds maximum depth")
    if isinstance(value, dict):
        return {key: render_template(child, context, depth + 1) for key, child in value.items()}
    if isinstance(value, list):
        return [render_template(child, context, depth + 1) for child in value]
    if not isinstance(value, str):
        return value
    full = TEMPLATE_RE.fullmatch(value)
    if full:
        return resolve_path(full.group(1).strip(), context)
    return TEMPLATE_RE.sub(lambda match: str(resolve_path(match.group(1).strip(), context)), value)
