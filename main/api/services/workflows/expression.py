"""Safe variable-path and string-template resolution (no eval/Jinja)."""

from __future__ import annotations

import re
from typing import Any, Dict


TEMPLATE_RE = re.compile(r"\$\{([^{}]+)\}")
SAFE_ROOTS = {"input", "steps", "run", "device"}
COMPARE_OPS = {"eq", "ne", "gt", "gte", "lt", "lte"}
STRING_OPS = {"contains", "startsWith", "endsWith"}


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


def evaluate_expression(expression: Any, context: Dict[str, Any], depth: int = 0) -> bool:
    """Evaluate the finite declarative expression language from Schema v1."""
    if depth > 16 or not isinstance(expression, dict):
        raise TemplateResolutionError("expression must be an object within maximum depth")
    op = str(expression.get("op") or "")
    if op in {"and", "or"}:
        children = expression.get("expressions", expression.get("args"))
        if not isinstance(children, list) or not children:
            raise TemplateResolutionError(f"{op} requires a non-empty expressions array")
        values = [evaluate_expression(child, context, depth + 1) for child in children]
        return all(values) if op == "and" else any(values)
    if op == "not":
        child = expression.get("expression", expression.get("value"))
        return not evaluate_expression(child, context, depth + 1)
    if op == "exists":
        value = expression.get("value", expression.get("left"))
        try:
            resolved = render_template(value, context, depth + 1)
        except TemplateResolutionError:
            return False
        return resolved is not None

    left = render_template(expression.get("left"), context, depth + 1)
    right = render_template(expression.get("right"), context, depth + 1)
    if op == "eq":
        return left == right
    if op == "ne":
        return left != right
    if op in {"gt", "gte", "lt", "lte"}:
        try:
            if op == "gt":
                return left > right
            if op == "gte":
                return left >= right
            if op == "lt":
                return left < right
            return left <= right
        except TypeError as exc:
            raise TemplateResolutionError(f"values are not comparable for {op}") from exc
    if op in STRING_OPS:
        if op == "contains":
            try:
                return right in left
            except TypeError as exc:
                raise TemplateResolutionError("contains operands are incompatible") from exc
        if not isinstance(left, str) or not isinstance(right, str):
            raise TemplateResolutionError(f"{op} requires string operands")
        return left.startswith(right) if op == "startsWith" else left.endswith(right)
    raise TemplateResolutionError(f"unsupported expression operator: {op}")
