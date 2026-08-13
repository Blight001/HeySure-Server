"""Safe variable-path and string-template resolution (no eval/Jinja)."""

from __future__ import annotations

import re
from typing import Any, Dict, Iterable, List, Union


TEMPLATE_RE = re.compile(r"\$\{([^{}]+)\}")
SAFE_ROOTS = {"input", "steps", "run", "device"}
COMPARE_OPS = {"eq", "ne", "gt", "gte", "lt", "lte"}
STRING_OPS = {"contains", "startsWith", "endsWith"}
MATCH_FIELDS = ("text", "name", "ariaLabel", "placeholder")


class TemplateResolutionError(ValueError):
    pass


PathToken = Union[str, int]


def parse_path(path: str) -> List[PathToken]:
    """Parse a safe dotted path with optional non-negative list indexes."""
    raw = str(path or "").strip()
    if not raw:
        raise TemplateResolutionError(f"forbidden variable path: {path}")
    tokens: List[PathToken] = []
    for segment in raw.split("."):
        if not segment:
            raise TemplateResolutionError(f"forbidden variable path: {path}")
        position = 0
        while position < len(segment):
            if segment[position] == "[":
                match = re.match(r"\[(0|[1-9][0-9]*)\]", segment[position:])
                if match is None:
                    raise TemplateResolutionError(f"invalid list index in variable path: {path}")
                tokens.append(int(match.group(1)))
                position += len(match.group(0))
                continue
            next_bracket = segment.find("[", position)
            end = len(segment) if next_bracket < 0 else next_bracket
            key = segment[position:end]
            if not key or "]" in key or key.startswith("__"):
                raise TemplateResolutionError(f"forbidden variable path: {path}")
            tokens.append(key)
            position = end
    if not tokens or tokens[0] not in SAFE_ROOTS or len(tokens) > 16:
        raise TemplateResolutionError(f"forbidden variable path: {path}")
    return tokens


def resolve_path(path: str, context: Dict[str, Any]) -> Any:
    parts = parse_path(path)
    current: Any = context
    traversed: List[str] = []
    for part in parts:
        if isinstance(part, int):
            if not isinstance(current, list):
                location = "".join(traversed) or "<root>"
                raise TemplateResolutionError(
                    f"unavailable variable path: {path} (expected list at {location})"
                )
            if part >= len(current):
                location = "".join(traversed) or "<root>"
                raise TemplateResolutionError(
                    f"unavailable variable path: {path} "
                    f"(index {part} out of range at {location}, length {len(current)})"
                )
            current = current[part]
            traversed.append(f"[{part}]")
            continue
        if not isinstance(current, dict) or part not in current:
            location = "".join(traversed) or "<root>"
            raise TemplateResolutionError(
                f"unavailable variable path: {path} (missing key {part} at {location})"
            )
        current = current[part]
        traversed.append(("." if traversed else "") + part)
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


def _normalized(value: Any) -> str:
    return " ".join(str(value or "").split()).casefold()


def _target_matches(item: Dict[str, Any], resolver: Dict[str, Any]) -> bool:
    if resolver.get("kind") and str(item.get("kind") or "") != str(resolver["kind"]):
        return False
    if resolver.get("tag") and str(item.get("tag") or "").casefold() != str(resolver["tag"]).casefold():
        return False
    if not bool(resolver.get("allowDisabled", False)) and bool(item.get("disabled")):
        return False
    expected = _normalized(resolver.get("text"))
    fields: Iterable[str] = resolver.get("fields") or MATCH_FIELDS
    values = [_normalized(item.get(field)) for field in fields]
    return not expected or (expected in values if resolver.get("exact", True) else any(expected in value for value in values))


def resolve_target_arguments(
    step: Dict[str, Any], context: Dict[str, Any], arguments: Dict[str, Any],
) -> Dict[str, Any]:
    resolver = step.get("targetResolver")
    if resolver is None:
        return arguments
    if not isinstance(resolver, dict):
        raise ValueError("targetResolver must be an object")
    rendered = render_template(resolver, context)
    items = rendered.get("items")
    if not isinstance(items, list):
        raise ValueError("targetResolver.items must resolve to an observation items array")
    matches = [item for item in items if isinstance(item, dict) and _target_matches(item, rendered)]
    if len(matches) != 1:
        raise ValueError(f"targetResolver expected exactly one element, found {len(matches)}")
    target = matches[0]
    resolved = dict(arguments)
    target_id, selector = str(target.get("id") or "").strip(), str(target.get("selector") or "").strip()
    if target_id:
        resolved["ref"] = target_id
        resolved.pop("selector", None)
    elif selector:
        resolved["selector"] = selector
        resolved.pop("ref", None)
    else:
        raise ValueError("targetResolver matched an element without ref or selector")
    return resolved


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
