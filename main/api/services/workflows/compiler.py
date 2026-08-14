"""Static compiler for the bounded, declarative workflow Schema v1."""

from __future__ import annotations

import hashlib
import json
import re
from copy import deepcopy
from typing import Any, Dict, Iterable, List, Set

from jsonschema import Draft202012Validator

from .expression import TemplateResolutionError, parse_path


STEP_ID_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_-]{0,63}$")
SAVE_AS_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_]{0,63}$")
TEMPLATE_RE = re.compile(r"\$\{([^{}]+)\}")
ALLOWED_NAMESPACES = {"input", "steps", "run", "device"}
EXPRESSION_OPS = {
    "eq", "ne", "gt", "gte", "lt", "lte", "exists", "contains",
    "startsWith", "endsWith", "and", "or", "not",
}
MAX_DEFINITION_BYTES = 256 * 1024
MAX_STEPS = 100
MAX_DEPTH = 16
SENSITIVE_FIELD_NAMES = {
    "authorization", "cookie", "password", "secret", "token", "api_key", "apikey",
}
INITIAL_ENVIRONMENT_REQUIREMENTS = (
    "browser workflow compatibility.initialEnvironment requires all three fields: "
    "description (non-empty), resetStepId (an existing browser+tab reload/replace step; "
    "replace also requires arguments.url), and readyStepId (an existing browser+wait/observe step). "
    "The startStepId chain must reach resetStepId, and resetStepId must reach readyStepId before normal actions"
)


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


def _template_ref_error(ref: str) -> str:
    parts = ref.split(".")
    if not parts or parts[0] not in ALLOWED_NAMESPACES:
        return f"template reference uses an unknown namespace: {ref}"
    if any(not item or item.startswith("__") for item in parts):
        return f"template reference contains a forbidden path segment: {ref}"
    if len(parts) > MAX_DEPTH:
        return f"template reference is too deep: {ref}"
    try:
        parse_path(ref)
    except TemplateResolutionError as exc:
        return str(exc)
    return ""


def _walk_templates(value: Any, path: str = "definition", depth: int = 0):
    if depth > MAX_DEPTH:
        yield path, "template value exceeds maximum nesting depth"
        return
    if depth == 0 and isinstance(value, dict):
        for message in _initial_environment_errors(value):
            yield "definition.compatibility.initialEnvironment", message
    if isinstance(value, dict):
        for key, child in value.items():
            yield from _walk_templates(child, f"{path}.{key}", depth + 1)
    elif isinstance(value, list):
        for index, child in enumerate(value):
            yield from _walk_templates(child, f"{path}[{index}]", depth + 1)
    elif isinstance(value, str):
        for match in TEMPLATE_RE.finditer(value):
            ref = match.group(1).strip()
            error = _template_ref_error(ref)
            if error:
                yield path, error


def _template_refs(value: Any) -> Set[str]:
    return {match.group(1).strip() for match in TEMPLATE_RE.finditer(canonical_json(value))}


def _tool_name(step: Any) -> str:
    return str(step.get("toolRef", {}).get("name") or "") if isinstance(step, dict) else ""


def _step_diagnostic(step_id: str, step: Any) -> str:
    if not isinstance(step, dict):
        return f"step {step_id or '(empty)'} (missing)"
    tool = _tool_name(step) or "missing toolRef.name"
    arguments = step.get("arguments") if isinstance(step.get("arguments"), dict) else {}
    action = str(arguments.get("action") or "missing")
    return f"step {step_id or '(empty)'} ({tool}, action={action})"


def _reset_step_errors(step_id: str, step: Any) -> List[str]:
    if not isinstance(step, dict):
        return [f"resetStepId must reference an existing step; got {_step_diagnostic(step_id, step)}"]
    action = str(step.get("arguments", {}).get("action") or "")
    errors = []
    if not _tool_name(step).endswith("browser+tab") or action not in {"reload", "replace"}:
        message = (
            "reset step must call browser+tab with action reload or replace; "
            f"got {_step_diagnostic(step_id, step)}"
        )
        if _tool_name(step).endswith("browser+tab") and action == "navigate":
            message += "; suggested fix: change action to replace and keep arguments.url"
        errors.append(message)
    if action == "replace" and not str(step.get("arguments", {}).get("url") or "").strip():
        errors.append(f"reset step {_step_diagnostic(step_id, step)} requires arguments.url")
    return errors


def _ready_step_errors(step_id: str, step: Any) -> List[str]:
    if not isinstance(step, dict):
        return [f"readyStepId must reference an existing step; got {_step_diagnostic(step_id, step)}"]
    if _tool_name(step).endswith("browser+wait") or _tool_name(step).endswith("browser+observe"):
        return []
    return [f"ready step {_step_diagnostic(step_id, step)} must call browser+wait or browser+observe"]


def _follows_next_chain(steps: Dict[str, Any], source: str, target: str) -> bool:
    current, seen = source, set()
    while current and current not in seen:
        if current == target:
            return True
        seen.add(current)
        step = steps.get(current)
        current = str(step.get("next") or "") if isinstance(step, dict) else ""
    return False


def _initial_topology_errors(
    definition: Dict[str, Any], steps: Dict[str, Any], reset_id: str, ready_id: str,
) -> List[str]:
    errors = []
    if reset_id in steps and ready_id in steps and not _follows_next_chain(steps, reset_id, ready_id):
        errors.append("reset step must reach ready step through the initialization next chain")
    start_id = str(definition.get("startStepId") or "")
    if reset_id in steps and not _follows_next_chain(steps, start_id, reset_id):
        errors.append("reset step must be reachable from startStepId before normal workflow actions")
    return errors


def _initial_environment_errors(definition: Dict[str, Any]) -> List[str]:
    steps = definition.get("steps") if isinstance(definition.get("steps"), dict) else {}
    if not any(".browser+" in _tool_name(step) for step in steps.values()):
        return []
    compatibility = definition.get("compatibility", {})
    contract = compatibility.get("initialEnvironment") if isinstance(compatibility, dict) else None
    if contract is None:
        return [INITIAL_ENVIRONMENT_REQUIREMENTS]
    if not isinstance(contract, dict):
        return [INITIAL_ENVIRONMENT_REQUIREMENTS, "initialEnvironment must be an object"]
    errors = []
    if not str(contract.get("description") or "").strip():
        errors.append("description is required")
    reset_id = str(contract.get("resetStepId") or "")
    ready_id = str(contract.get("readyStepId") or "")
    errors.extend(_reset_step_errors(reset_id, steps.get(reset_id)))
    errors.extend(_ready_step_errors(ready_id, steps.get(ready_id)))
    errors.extend(_initial_topology_errors(definition, steps, reset_id, ready_id))
    return [INITIAL_ENVIRONMENT_REQUIREMENTS, *errors] if errors else []


def _validate_expression(expression: Any, path: str, errors: List[str], depth: int = 0) -> None:
    if depth > MAX_DEPTH or not isinstance(expression, dict):
        errors.append(f"{path}: expression must be an object within maximum depth")
        return
    op = expression.get("op")
    if op not in EXPRESSION_OPS:
        errors.append(f"{path}: unsupported expression operator {op}")
        return
    if op in {"and", "or"}:
        children = expression.get("expressions", expression.get("args"))
        if not isinstance(children, list) or not children:
            errors.append(f"{path}: {op} requires a non-empty expressions array")
        else:
            for index, child in enumerate(children):
                _validate_expression(child, f"{path}.{op}[{index}]", errors, depth + 1)
    elif op == "not":
        _validate_expression(
            expression.get("expression", expression.get("value")), f"{path}.not", errors, depth + 1
        )
    elif op == "exists":
        if "value" not in expression and "left" not in expression:
            errors.append(f"{path}: exists requires value")
    elif "left" not in expression or "right" not in expression:
        errors.append(f"{path}: {op} requires left and right")


def _walk_sensitive_literals(value: Any, path: str = "definition", depth: int = 0):
    if depth > MAX_DEPTH:
        return
    if isinstance(value, dict):
        for key, child in value.items():
            child_path = f"{path}.{key}"
            if str(key).lower() in SENSITIVE_FIELD_NAMES and not isinstance(child, (dict, list)):
                text = str(child or "")
                if text and not TEMPLATE_RE.fullmatch(text):
                    yield child_path
            yield from _walk_sensitive_literals(child, child_path, depth + 1)
    elif isinstance(value, list):
        for index, child in enumerate(value):
            yield from _walk_sensitive_literals(child, f"{path}[{index}]", depth + 1)


def _validate_ai_step(
    step_id: str,
    step: Dict[str, Any],
    errors: List[str],
    save_names: Set[str],
    targets: List[str],
) -> None:
    if not str(step.get("prompt") or "").strip():
        errors.append(f"step {step_id}: prompt is required")
    save_as = str(step.get("saveAs") or "")
    if not SAVE_AS_RE.fullmatch(save_as):
        errors.append(f"step {step_id}: saveAs is required and must be a safe identifier")
    elif save_as in save_names:
        errors.append(f"step {step_id}: duplicate saveAs {save_as}")
    else:
        save_names.add(save_as)
    target = str(step.get("next") or "")
    if target:
        targets.append(target)
    else:
        errors.append(f"step {step_id}: next is required")
    on_error = step.get("onError", "fail")
    if isinstance(on_error, str) and on_error not in {"", "fail"}:
        targets.append(on_error)
    elif not isinstance(on_error, str):
        errors.append(f"step {step_id}: onError must be fail or a step id")
    review_timeout = step.get("timeoutSeconds", 300)
    if not isinstance(review_timeout, int) or not 1 <= review_timeout <= 86400:
        errors.append(f"step {step_id}: timeoutSeconds must be 1..86400")


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
        if step_type not in {"mcp", "condition", "delay", "ai", "end"}:
            errors.append(f"step {step_id}: unsupported step type {step_type}")
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
            total_timeout = step.get("totalTimeoutSeconds", timeout)
            if not isinstance(total_timeout, int) or not timeout <= total_timeout <= 86400:
                errors.append(f"step {step_id}: totalTimeoutSeconds must be >= timeoutSeconds and <= 86400")
            projection = step.get("resultProjection")
            if projection is not None:
                if not isinstance(projection, list) or not all(isinstance(item, str) for item in projection):
                    errors.append(f"step {step_id}: resultProjection must be an array of field paths")
                else:
                    for item in projection:
                        parts = item.split(".")
                        if not item or len(parts) > MAX_DEPTH or any(not part or part.startswith("__") for part in parts):
                            errors.append(f"step {step_id}: invalid resultProjection path {item}")
                        if any(part.lower() in SENSITIVE_FIELD_NAMES for part in parts):
                            errors.append(f"step {step_id}: resultProjection cannot persist sensitive field {item}")
            on_error = step.get("onError", "fail")
            if isinstance(on_error, str) and on_error not in {"", "fail"}:
                targets.append(on_error)
            elif not isinstance(on_error, str):
                errors.append(f"step {step_id}: onError must be fail or a step id")
            retry = step.get("retryPolicy")
            if retry is not None:
                if not isinstance(retry, dict):
                    errors.append(f"step {step_id}: retryPolicy must be an object")
                else:
                    attempts = retry.get("maxAttempts", 1)
                    delay_seconds = retry.get("delaySeconds", 1)
                    max_delay = retry.get("maxDelaySeconds", 60)
                    mode = retry.get("backoff", "fixed")
                    if not isinstance(attempts, int) or not 1 <= attempts <= 10:
                        errors.append(f"step {step_id}: retryPolicy.maxAttempts must be 1..10")
                    if not isinstance(delay_seconds, (int, float)) or not 0 <= delay_seconds <= 3600:
                        errors.append(f"step {step_id}: retryPolicy.delaySeconds must be 0..3600")
                    if not isinstance(max_delay, (int, float)) or not 0 <= max_delay <= 3600:
                        errors.append(f"step {step_id}: retryPolicy.maxDelaySeconds must be 0..3600")
                    if mode not in {"fixed", "exponential"}:
                        errors.append(f"step {step_id}: retryPolicy.backoff must be fixed or exponential")
        elif step_type == "ai":
            _validate_ai_step(str(step_id), step, errors, save_names, targets)
        elif step_type == "condition":
            _validate_expression(step.get("expression"), f"step {step_id}.expression", errors)
            for field in ("onTrue", "onFalse"):
                target = str(step.get(field) or "")
                if not target:
                    errors.append(f"step {step_id}: {field} is required")
                else:
                    targets.append(target)
        elif step_type == "delay":
            seconds = step.get("delaySeconds", step.get("seconds"))
            if not isinstance(seconds, (int, float)) or not 0 <= seconds <= 86400:
                errors.append(f"step {step_id}: delaySeconds must be 0..86400")
            target = str(step.get("next") or "")
            if not target:
                errors.append(f"step {step_id}: next is required")
            else:
                targets.append(target)
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

    # A step result may only be referenced after its producer on every path.
    # This conservative dominator check prevents runtime reads of a result that
    # exists on only one side of a branch.
    producers = {
        str(step.get("saveAs")): step_id
        for step_id, step in steps.items()
        if isinstance(step, dict) and step.get("type") in {"mcp", "ai"} and step.get("saveAs")
    }
    predecessors: Dict[str, Set[str]] = {step_id: set() for step_id in steps}
    for source, targets in edges.items():
        for target in targets:
            if target in predecessors:
                predecessors[target].add(source)
    dominators: Dict[str, Set[str]] = {step_id: set(steps) for step_id in steps}
    if start in steps:
        dominators[start] = {start}
        changed = True
        while changed:
            changed = False
            for step_id in reachable - {start}:
                incoming = [dominators[parent] for parent in predecessors[step_id] if parent in reachable]
                new_value = {step_id} | (set.intersection(*incoming) if incoming else set())
                if new_value != dominators[step_id]:
                    dominators[step_id] = new_value
                    changed = True
    for step_id in reachable:
        for ref in _template_refs(steps[step_id]):
            parts = ref.split(".")
            if len(parts) >= 2 and parts[0] == "steps":
                producer = producers.get(parts[1])
                if producer and producer not in dominators.get(step_id, set()):
                    errors.append(
                        f"step {step_id}: template may read step result before it is available: {ref}"
                    )
    reachable_ends = [step_id for step_id in reachable if steps[step_id].get("type") == "end"]
    for ref in _template_refs(definition.get("output", {})):
        parts = ref.split(".")
        if len(parts) >= 2 and parts[0] == "steps":
            producer = producers.get(parts[1])
            if producer and any(producer not in dominators.get(end_id, set()) for end_id in reachable_ends):
                errors.append(f"output may read step result unavailable on an end path: {ref}")

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
    max_result_bytes = limits.get("maxResultBytes", 10 * 1024 * 1024)
    if not isinstance(max_result_bytes, int) or not 1024 <= max_result_bytes <= 100 * 1024 * 1024:
        errors.append("limits.maxResultBytes must be 1024..104857600")
    if isinstance(timeout, int):
        for step_id, step in steps.items():
            if isinstance(step, dict) and isinstance(step.get("timeoutSeconds"), int) and step["timeoutSeconds"] > timeout:
                errors.append(f"step {step_id}: timeoutSeconds exceeds workflow timeout")
            if isinstance(step, dict) and isinstance(step.get("totalTimeoutSeconds"), int) and step["totalTimeoutSeconds"] > timeout:
                errors.append(f"step {step_id}: totalTimeoutSeconds exceeds workflow timeout")

    for path, message in _walk_templates(definition):
        errors.append(f"{path}: {message}")
    input_properties = input_schema.get("properties", {}) if isinstance(input_schema, dict) else {}
    for match in TEMPLATE_RE.finditer(canonical_json(definition)):
        ref = match.group(1).strip()
        parts = ref.split(".")
        if len(parts) >= 2 and parts[0] == "input" and parts[1] not in input_properties:
            errors.append(f"template references undeclared input property: {ref}")
        if len(parts) >= 2 and parts[0] == "steps":
            if parts[1] not in save_names:
                errors.append(f"template references undeclared step result: {ref}")
            elif len(parts) < 3 or parts[2] not in {"result", "error"}:
                errors.append(f"step template must read result or error: {ref}")
    for path in _walk_sensitive_literals(definition):
        # inputSchema merely declares a sensitive field; only runtime values
        # belong in input, never literal secrets in a workflow definition.
        if ".inputSchema.properties." not in path:
            errors.append(f"definition contains a literal sensitive value at {path}; use an input reference")
    if errors:
        raise WorkflowValidationError(errors, warnings)

    normalized = deepcopy(definition)
    normalized.setdefault("inputSchema", {"type": "object"})
    normalized.setdefault("limits", {})
    normalized["limits"].setdefault("timeoutSeconds", 300)
    normalized["limits"].setdefault("maxTransitions", min(MAX_STEPS, 100))
    normalized["limits"].setdefault("maxResultBytes", 10 * 1024 * 1024)
    normalized.setdefault("output", {})
    return {"definition": normalized, "digest": definition_digest(normalized), "warnings": warnings}
