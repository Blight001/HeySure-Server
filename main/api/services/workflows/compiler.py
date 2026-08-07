"""Static compiler for the deliberately small workflow Schema v1.

Phase one executes only ``mcp`` and ``end`` nodes. Unsupported node types are
rejected at publish time instead of being partially interpreted at runtime.
"""

from __future__ import annotations

import hashlib
import json
import re
from copy import deepcopy
from typing import Any, Dict, Iterable, List, Set

from jsonschema import Draft202012Validator


STEP_ID_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_-]{0,63}$")
SAVE_AS_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_]{0,63}$")
TEMPLATE_RE = re.compile(r"\$\{([^{}]+)\}")
ALLOWED_NAMESPACES = {"input", "steps", "run", "device"}
MAX_DEFINITION_BYTES = 256 * 1024
MAX_STEPS = 100
MAX_DEPTH = 16


class WorkflowValidationError(ValueError):
    def __init__(self, errors: Iterable[str], warnings: Iterable[str] = ()):
        self.errors = list(errors)
        self.warnings = list(warnings)
        super().__init__("; ".join(self.errors))


def canonical_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def definition_digest(value: Any) -> str:
    return "sha256:" + hashlib.sha256(canonical_json(value).encode("utf-8")).hexdigest()


def schema_digest(value: Any) -> str:
    return definition_digest(value if isinstance(value, dict) else {})


def _walk_templates(value: Any, path: str = "definition", depth: int = 0):
    if depth > MAX_DEPTH:
        yield path, "template value exceeds maximum nesting depth"
        return
    if isinstance(value, dict):
        for key, child in value.items():
            yield from _walk_templates(child, f"{path}.{key}", depth + 1)
    elif isinstance(value, list):
        for index, child in enumerate(value):
            yield from _walk_templates(child, f"{path}[{index}]", depth + 1)
    elif isinstance(value, str):
        for match in TEMPLATE_RE.finditer(value):
            ref = match.group(1).strip()
            parts = ref.split(".")
            if not parts or parts[0] not in ALLOWED_NAMESPACES:
                yield path, f"template reference uses an unknown namespace: {ref}"
            elif any(not item or item.startswith("__") for item in parts):
                yield path, f"template reference contains a forbidden path segment: {ref}"
            elif len(parts) > MAX_DEPTH:
                yield path, f"template reference is too deep: {ref}"


def compile_definition(definition: Dict[str, Any]) -> Dict[str, Any]:
    errors: List[str] = []
    warnings: List[str] = []
    if not isinstance(definition, dict):
        raise WorkflowValidationError(["definition must be an object"])
    if len(canonical_json(definition).encode("utf-8")) > MAX_DEFINITION_BYTES:
        errors.append(f"definition exceeds {MAX_DEFINITION_BYTES} bytes")
    if definition.get("schemaVersion") != 1:
        errors.append("schemaVersion must be 1")

    input_schema = definition.get("inputSchema", {"type": "object"})
    if not isinstance(input_schema, dict):
        errors.append("inputSchema must be an object")
    else:
        try:
            Draft202012Validator.check_schema(input_schema)
        except Exception as exc:
            errors.append(f"inputSchema is invalid: {exc}")

    steps = definition.get("steps")
    if not isinstance(steps, dict) or not steps:
        errors.append("steps must be a non-empty object")
        steps = {}
    if len(steps) > MAX_STEPS:
        errors.append(f"steps exceeds maximum of {MAX_STEPS}")
    start = str(definition.get("startStepId") or "")
    if start not in steps:
        errors.append("startStepId must reference an existing step")

    save_names: Set[str] = set()
    edges: Dict[str, List[str]] = {}
    for step_id, step in steps.items():
        if not STEP_ID_RE.fullmatch(str(step_id)):
            errors.append(f"invalid step id: {step_id}")
            continue
        if not isinstance(step, dict):
            errors.append(f"step {step_id} must be an object")
            continue
        step_type = step.get("type")
        if step_type not in {"mcp", "end"}:
            errors.append(f"step {step_id}: Schema v1 phase one supports only mcp and end")
            continue
        targets: List[str] = []
        if step_type == "mcp":
            ref = step.get("toolRef")
            if not isinstance(ref, dict) or not str(ref.get("name") or "").strip():
                errors.append(f"step {step_id}: toolRef.name is required")
            elif str(ref.get("namespace") or "device") != "device":
                errors.append(f"step {step_id}: toolRef.namespace must be device")
            if not isinstance(step.get("arguments", {}), dict):
                errors.append(f"step {step_id}: arguments must be an object")
            save_as = str(step.get("saveAs") or "")
            if not SAVE_AS_RE.fullmatch(save_as):
                errors.append(f"step {step_id}: saveAs is required and must be a safe identifier")
            elif save_as in save_names:
                errors.append(f"step {step_id}: duplicate saveAs {save_as}")
            else:
                save_names.add(save_as)
            target = str(step.get("next") or "")
            if not target:
                errors.append(f"step {step_id}: next is required")
            else:
                targets.append(target)
            timeout = step.get("timeoutSeconds", 120)
            if not isinstance(timeout, int) or not 1 <= timeout <= 1800:
                errors.append(f"step {step_id}: timeoutSeconds must be 1..1800")
            projection = step.get("resultProjection")
            if projection is not None:
                if not isinstance(projection, list) or not all(isinstance(item, str) for item in projection):
                    errors.append(f"step {step_id}: resultProjection must be an array of field paths")
                else:
                    for item in projection:
                        parts = item.split(".")
                        if not item or len(parts) > MAX_DEPTH or any(not part or part.startswith("__") for part in parts):
                            errors.append(f"step {step_id}: invalid resultProjection path {item}")
        edges[str(step_id)] = targets

    for source, targets in edges.items():
        for target in targets:
            if target not in steps:
                errors.append(f"step {source}: transition target {target} does not exist")

    reachable: Set[str] = set()
    stack = [start] if start in steps else []
    while stack:
        node = stack.pop()
        if node in reachable:
            continue
        reachable.add(node)
        stack.extend(edges.get(node, ()))
    unreachable = sorted(set(steps) - reachable)
    if unreachable:
        warnings.append("unreachable steps: " + ", ".join(unreachable))
    if reachable and not any(steps[node].get("type") == "end" for node in reachable):
        errors.append("workflow has no reachable end step")

    visiting: Set[str] = set()
    visited: Set[str] = set()
    def visit(node: str) -> None:
        if node in visiting:
            errors.append(f"workflow contains a cycle through step {node}")
            return
        if node in visited:
            return
        visiting.add(node)
        for target in edges.get(node, ()):
            if target in steps:
                visit(target)
        visiting.remove(node)
        visited.add(node)
    if start in steps:
        visit(start)

    limits = definition.get("limits", {})
    if not isinstance(limits, dict):
        errors.append("limits must be an object")
        limits = {}
    timeout = limits.get("timeoutSeconds", 300)
    transitions = limits.get("maxTransitions", min(MAX_STEPS, 100))
    if not isinstance(timeout, int) or not 1 <= timeout <= 86400:
        errors.append("limits.timeoutSeconds must be 1..86400")
    if not isinstance(transitions, int) or not 1 <= transitions <= 500:
        errors.append("limits.maxTransitions must be 1..500")
    if isinstance(timeout, int):
        for step_id, step in steps.items():
            if isinstance(step, dict) and isinstance(step.get("timeoutSeconds"), int) and step["timeoutSeconds"] > timeout:
                errors.append(f"step {step_id}: timeoutSeconds exceeds workflow timeout")

    for path, message in _walk_templates(definition):
        errors.append(f"{path}: {message}")
    if errors:
        raise WorkflowValidationError(errors, warnings)

    normalized = deepcopy(definition)
    normalized.setdefault("inputSchema", {"type": "object"})
    normalized.setdefault("limits", {})
    normalized["limits"].setdefault("timeoutSeconds", 300)
    normalized["limits"].setdefault("maxTransitions", min(MAX_STEPS, 100))
    normalized.setdefault("output", {})
    return {"definition": normalized, "digest": definition_digest(normalized), "warnings": warnings}
