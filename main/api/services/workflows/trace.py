"""Safe conversion of explicit structured MCP calls into a review-only draft."""

from typing import Any, Dict

from .compiler import WorkflowValidationError


SENSITIVE_KEYS = {"authorization", "cookie", "password", "secret", "token", "api_key", "apikey"}


def _call_tool(call: Dict[str, Any]) -> str:
    return str(call.get("tool") or call.get("name") or "")


def _is_reset_call(call: Dict[str, Any]) -> bool:
    arguments = call.get("arguments") if isinstance(call.get("arguments"), dict) else {}
    return _call_tool(call).endswith("browser+tab") and arguments.get("action") in {"reload", "replace"}


def _is_ready_call(call: Dict[str, Any]) -> bool:
    return _call_tool(call).endswith(("browser+wait", "browser+observe"))


def _initial_environment(calls: list[Dict[str, Any]]) -> Dict[str, Any]:
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
            "resetStepId": f"call_{reset_index}",
            "readyStepId": f"call_{ready_index}",
        }
    }


def _tool_ref(call: Dict[str, Any], tool_name: str) -> Dict[str, Any]:
    ref = {"namespace": "device", "name": tool_name}
    device_id = str(call.get("device_id") or call.get("deviceId") or "").strip()
    ref.update({"deviceId": device_id} if device_id else {})
    digest = str(call.get("schemaDigest") or "").strip()
    ref.update({"schemaDigest": digest} if digest else {})
    return ref


def definition_from_trace(calls: list[Dict[str, Any]], *, name: str, description: str = "") -> Dict[str, Any]:
    if not calls or len(calls) > 50:
        raise WorkflowValidationError(["trace must contain 1..50 MCP calls"])
    properties: Dict[str, Any] = {}
    required = []

    def parameterize(value: Any, step_number: int, path: list[str]) -> Any:
        if isinstance(value, dict):
            return {str(key): parameterize(child, step_number, path + [str(key)]) for key, child in value.items()}
        if isinstance(value, list):
            return [parameterize(child, step_number, path + [str(index)]) for index, child in enumerate(value)]
        if path and path[-1].lower() in SENSITIVE_KEYS:
            field = f"step_{step_number}_{'_'.join(path)}"
            field = "".join(char if char.isalnum() or char == "_" else "_" for char in field)[:64]
            properties[field] = {"type": "string", "title": ".".join(path), "writeOnly": True}
            required.append(field)
            return f"${{input.{field}}}"
        return value

    steps: Dict[str, Any] = {}
    for index, call in enumerate(calls, start=1):
        if not isinstance(call, dict):
            raise WorkflowValidationError([f"trace call {index} must be an object"])
        tool_name = str(call.get("tool") or call.get("name") or "").strip()
        if not tool_name:
            raise WorkflowValidationError([f"trace call {index} requires tool"])
        step_id = f"call_{index}"
        next_step = f"call_{index + 1}" if index < len(calls) else "finish"
        ref = _tool_ref(call, tool_name)
        steps[step_id] = {
            "type": "mcp",
            "toolRef": ref,
            "arguments": parameterize(call.get("arguments") if isinstance(call.get("arguments"), dict) else {}, index, []),
            "saveAs": f"call_{index}_result",
            "timeoutSeconds": min(1800, max(1, int(call.get("timeoutSeconds") or 120))),
            "next": next_step,
            "onError": "fail",
        }
    steps["finish"] = {"type": "end"}
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
        "startStepId": "call_1",
        "limits": {"timeoutSeconds": min(86400, max(300, len(calls) * 180)), "maxTransitions": len(calls) + 2},
        "steps": steps,
        "output": {"lastResult": f"${{steps.call_{len(calls)}_result.result}}"},
    }
    compatibility = _initial_environment(calls)
    if compatibility:
        definition["compatibility"] = compatibility
    return definition
