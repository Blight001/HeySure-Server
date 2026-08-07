"""Safe conversion of explicit structured MCP calls into a review-only draft."""

from typing import Any, Dict

from .compiler import WorkflowValidationError


SENSITIVE_KEYS = {"authorization", "cookie", "password", "secret", "token", "api_key", "apikey"}


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
        ref = {"namespace": "device", "name": tool_name}
        if call.get("schemaDigest"):
            ref["schemaDigest"] = str(call["schemaDigest"])
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
    return {
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
