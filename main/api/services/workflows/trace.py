"""Safe conversion of explicit structured MCP calls into a review-only draft."""

from __future__ import annotations

import re
from typing import Any, Dict

from .compiler import WorkflowValidationError
from .recording_trace_browser import prepare_browser_calls, stabilize_browser_refs


SENSITIVE_KEYS = {"authorization", "cookie", "password", "secret", "token", "api_key", "apikey"}
MAX_GENERATED_ID_LENGTH = 57  # Leaves room for the compiler-safe ``_result`` suffix.


def _call_tool(call: Dict[str, Any]) -> str:
    return str(call.get("tool") or call.get("name") or "")


def _is_reset_call(call: Dict[str, Any]) -> bool:
    arguments = call.get("arguments") if isinstance(call.get("arguments"), dict) else {}
    return _call_tool(call).endswith("browser+tab") and arguments.get("action") in {"reload", "replace"}


def _is_ready_call(call: Dict[str, Any]) -> bool:
    return _call_tool(call).endswith(("browser+wait", "browser+observe"))


def _identifier_part(value: Any) -> str:
    text = re.sub(r"[^A-Za-z0-9]+", "_", str(value or "").strip()).strip("_").lower()
    return text


def _semantic_step_ids(calls: list[Dict[str, Any]]) -> list[str]:
    """Build deterministic, compiler-safe ids from each tool and its action."""
    used = {"finish"}
    generated = []
    for call in calls:
        tool_part = _identifier_part(_call_tool(call)) or "tool"
        arguments = call.get("arguments") if isinstance(call.get("arguments"), dict) else {}
        action_part = _identifier_part(arguments.get("action"))
        base = "_".join(part for part in (tool_part, action_part) if part)
        if not base[0].isalpha():
            base = f"tool_{base}"
        base = base[:MAX_GENERATED_ID_LENGTH].rstrip("_") or "tool"
        candidate = base
        duplicate = 1
        while candidate in used:
            duplicate += 1
            suffix = f"_{duplicate}"
            candidate = f"{base[:MAX_GENERATED_ID_LENGTH - len(suffix)].rstrip('_')}{suffix}"
        used.add(candidate)
        generated.append(candidate)
    return generated


def _initial_environment(calls: list[Dict[str, Any]], step_ids: list[str]) -> Dict[str, Any]:
    if not any(".browser+" in _call_tool(call) for call in calls):
        return {}
    reset_index = next((index for index, call in enumerate(calls, start=1) if _is_reset_call(call)), None)
    ready_index = next((
        index for index, call in enumerate(calls, start=1)
        if reset_index is not None and index > reset_index and _is_ready_call(call)
    ), None)
    if reset_index is None or ready_index is None:
        return {}
    return {
        "initialEnvironment": {
            "description": "每次运行先重新加载或重置目标页面，并等待页面进入可操作状态，避免继承上次运行的页面状态。",
            "resetStepId": step_ids[reset_index - 1],
            "readyStepId": step_ids[ready_index - 1],
        }
    }


def _tool_ref(call: Dict[str, Any], tool_name: str) -> Dict[str, Any]:
    ref = {"namespace": "device", "name": tool_name}
    device_id = str(call.get("device_id") or call.get("deviceId") or "").strip()
    ref.update({"deviceId": device_id} if device_id else {})
    digest = str(call.get("schemaDigest") or "").strip()
    ref.update({"schemaDigest": digest} if digest else {})
    return ref


def _validate_calls(calls: list[Dict[str, Any]]) -> None:
    if not calls or len(calls) > 50:
        raise WorkflowValidationError(["trace must contain 1..50 MCP calls"])
    for index, call in enumerate(calls, start=1):
        if not isinstance(call, dict):
            raise WorkflowValidationError([f"trace call {index} must be an object"])
        if not _call_tool(call).strip():
            raise WorkflowValidationError([f"trace call {index} requires tool"])


def _parameterize(
    value: Any,
    step_number: int,
    path: list[str],
    properties: Dict[str, Any],
    required: list[str],
) -> Any:
    if isinstance(value, dict):
        return {
            str(key): _parameterize(child, step_number, path + [str(key)], properties, required)
            for key, child in value.items()
        }
    if isinstance(value, list):
        return [
            _parameterize(child, step_number, path + [str(index)], properties, required)
            for index, child in enumerate(value)
        ]
    if path and path[-1].lower() in SENSITIVE_KEYS:
        field = f"step_{step_number}_{'_'.join(path)}"
        field = "".join(char if char.isalnum() or char == "_" else "_" for char in field)[:64]
        properties[field] = {"type": "string", "title": ".".join(path), "writeOnly": True}
        required.append(field)
        return f"${{input.{field}}}"
    return value


def _trace_steps(
    calls: list[Dict[str, Any]],
    step_ids: list[str],
    save_names: list[str],
    target_resolvers: Dict[int, Dict[str, Any]],
    properties: Dict[str, Any],
    required: list[str],
) -> Dict[str, Any]:
    steps: Dict[str, Any] = {}
    for index, call in enumerate(calls, start=1):
        tool_name = str(call.get("tool") or call.get("name") or "").strip()
        step_id = step_ids[index - 1]
        next_step = step_ids[index] if index < len(calls) else "finish"
        ref = _tool_ref(call, tool_name)
        steps[step_id] = {
            "type": "mcp",
            "toolRef": ref,
            "arguments": _parameterize(
                call.get("arguments") if isinstance(call.get("arguments"), dict) else {},
                index, [], properties, required,
            ),
            "saveAs": save_names[index - 1],
            "timeoutSeconds": min(1800, max(1, int(call.get("timeoutSeconds") or 120))),
            "next": next_step,
            "onError": "fail",
        }
        if index - 1 in target_resolvers:
            steps[step_id]["targetResolver"] = target_resolvers[index - 1]
    steps["finish"] = {"type": "end"}
    return steps


def definition_from_trace(calls: list[Dict[str, Any]], *, name: str, description: str = "") -> Dict[str, Any]:
    _validate_calls(calls)
    calls, detached_warnings = prepare_browser_calls(calls)
    properties: Dict[str, Any] = {}
    required: list[str] = []
    step_ids = _semantic_step_ids(calls)
    save_names = [f"{step_id}_result" for step_id in step_ids]
    target_resolvers = stabilize_browser_refs(calls, save_names)
    steps = _trace_steps(calls, step_ids, save_names, target_resolvers, properties, required)
    definition = {
        "schemaVersion": 1,
        "name": name,
        "description": description,
        "inputSchema": {
            "type": "object",
            "properties": properties,
            "required": sorted(set(required)),
            "additionalProperties": False,
        },
        "startStepId": step_ids[0],
        "limits": {"timeoutSeconds": min(86400, max(300, len(calls) * 180)), "maxTransitions": len(calls) + 2},
        "steps": steps,
        "output": {"lastResult": f"${{steps.{save_names[-1]}.result}}"},
    }
    recording_warnings = detached_warnings + [
        {"code": code, "stepId": step_ids[index]}
        for index, call in enumerate(calls)
        for code in call.get("_recordingWarnings", [])
    ]
    if recording_warnings:
        definition["recordingWarnings"] = recording_warnings
    compatibility = _initial_environment(calls, step_ids)
    if compatibility:
        definition["compatibility"] = compatibility
    return definition
