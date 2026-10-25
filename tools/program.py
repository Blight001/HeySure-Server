"""Bounded MCP orchestration primitive.

``mcp.program`` is the first, protocol-compatible layer of the MCP
orchestrator.  It deliberately accepts a JSON workflow rather than arbitrary
JavaScript: the same contract works for every MCP client and gives us a safe
place to add a sandboxed JS frontend later.

The interpreter supports dependency-aware calls, bounded ``foreach`` fan-out,
variable references (``${input.foo}`` / ``${steps.fetch.result.items}``) and
bounded concurrency.  Every nested call is re-authorized before dispatch.
"""

from __future__ import annotations

import asyncio
import copy
import json
import re
import time
import uuid
from typing import Any, Callable, Dict, Iterable, Mapping, Optional

from fastapi import HTTPException

from api.core.settings import settings


PROGRAM_TOOL_NAME = "mcp.program"
_REFERENCE = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_.\[\]-]*)\}")


class ProgramValidationError(ValueError):
    """A deterministic, client-readable program validation error."""

    def __init__(self, errors: Iterable[str]):
        self.errors = [str(item) for item in errors if str(item).strip()]
        super().__init__("; ".join(self.errors) or "invalid MCP program")


class ProgramCancelled(RuntimeError):
    """Raised at a safe step boundary when an async run is cancelled."""


def _json_size(value: Any) -> int:
    return len(json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode("utf-8"))


def _path_parts(path: str) -> list[str]:
    parts: list[str] = []
    for part in path.split("."):
        if not part:
            raise KeyError(path)
        while "[" in part:
            head, tail = part.split("[", 1)
            if head:
                parts.append(head)
            index, tail = tail.split("]", 1)
            if not index.isdigit():
                raise KeyError(path)
            parts.append(index)
            part = tail
        if part:
            parts.append(part)
    return parts


def resolve_reference(path: str, context: Mapping[str, Any]) -> Any:
    """Resolve a bounded dotted/indexed reference from the program context."""

    current: Any = context
    for part in _path_parts(path):
        if isinstance(current, Mapping):
            if part not in current:
                raise KeyError(path)
            current = current[part]
        elif isinstance(current, list) and part.isdigit():
            current = current[int(part)]
        else:
            raise KeyError(path)
    return current


def render_value(value: Any, context: Mapping[str, Any]) -> Any:
    """Render references recursively while preserving exact-value types."""

    if isinstance(value, str):
        exact = _REFERENCE.fullmatch(value.strip())
        if exact:
            try:
                return copy.deepcopy(resolve_reference(exact.group(1), context))
            except KeyError as exc:
                raise ProgramValidationError([f"unresolved reference: {exact.group(1)}"]) from exc

        def replace(match: re.Match[str]) -> str:
            try:
                resolved = resolve_reference(match.group(1), context)
            except KeyError as exc:
                raise ProgramValidationError([f"unresolved reference: {match.group(1)}"]) from exc
            if isinstance(resolved, (dict, list)):
                return json.dumps(resolved, ensure_ascii=False, separators=(",", ":"))
            return str(resolved)

        return _REFERENCE.sub(replace, value)
    if isinstance(value, list):
        return [render_value(item, context) for item in value]
    if isinstance(value, dict):
        return {str(key): render_value(item, context) for key, item in value.items()}
    return value


def _step_dependencies(step: Mapping[str, Any]) -> set[str]:
    dependencies = step.get("depends_on", [])
    if dependencies is None:
        return set()
    if not isinstance(dependencies, list) or not all(isinstance(item, str) for item in dependencies):
        raise ProgramValidationError([f"step {step.get('id', '<unknown>')}: depends_on must be a string array"])
    return {item.strip() for item in dependencies if item.strip()}


def validate_program(program: Any) -> Dict[str, Any]:
    """Validate and normalize the phase-one JSON workflow contract."""

    if not isinstance(program, dict):
        raise ProgramValidationError(["program must be an object"])
    steps = program.get("steps")
    if not isinstance(steps, list) or not steps:
        raise ProgramValidationError(["program.steps must be a non-empty array"])
    if len(steps) > int(settings.mcp_program_max_steps):
        raise ProgramValidationError([f"program has more than {settings.mcp_program_max_steps} steps"])

    seen: set[str] = set()
    normalized: list[Dict[str, Any]] = []
    errors: list[str] = []
    for index, raw in enumerate(steps):
        if not isinstance(raw, dict):
            errors.append(f"steps[{index}] must be an object")
            continue
        step = dict(raw)
        step_id = str(step.get("id") or "").strip()
        tool = str(step.get("tool") or "").strip()
        if not step_id or not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_-]{0,63}", step_id):
            errors.append(f"steps[{index}].id is invalid")
        elif step_id in seen:
            errors.append(f"duplicate step id: {step_id}")
        seen.add(step_id)
        if not tool or tool == PROGRAM_TOOL_NAME:
            errors.append(f"steps[{index}].tool is invalid")
        arguments = step.get("arguments", {})
        if not isinstance(arguments, dict):
            errors.append(f"step {step_id or index}: arguments must be an object")
        try:
            dependencies = _step_dependencies(step)
        except ProgramValidationError as exc:
            errors.extend(exc.errors)
            dependencies = set()
        foreach = step.get("foreach")
        if foreach is not None and (not isinstance(foreach, str) or not foreach.strip()):
            errors.append(f"step {step_id or index}: foreach must be a reference string")
        concurrency = step.get("concurrency", settings.mcp_program_max_concurrency)
        if not isinstance(concurrency, int) or not 1 <= concurrency <= settings.mcp_program_max_concurrency:
            errors.append(
                f"step {step_id or index}: concurrency must be 1..{settings.mcp_program_max_concurrency}"
            )
        on_error = str(step.get("on_error") or "fail").strip().lower()
        if on_error not in {"fail", "continue"}:
            errors.append(f"step {step_id or index}: on_error must be fail or continue")
        step["id"] = step_id
        step["tool"] = tool
        step["arguments"] = arguments
        step["depends_on"] = sorted(dependencies)
        step["concurrency"] = concurrency
        step["on_error"] = on_error
        normalized.append(step)

    ids = {step.get("id") for step in normalized}
    for step in normalized:
        missing = set(step["depends_on"]) - ids
        if missing:
            errors.append(f"step {step['id']}: unknown dependency {sorted(missing)}")
    if errors:
        raise ProgramValidationError(errors)

    # A topological check catches cycles before any tool has side effects.
    remaining = {step["id"]: set(step["depends_on"]) for step in normalized}
    resolved: set[str] = set()
    while remaining:
        ready = {step_id for step_id, deps in remaining.items() if deps <= resolved}
        if not ready:
            raise ProgramValidationError(["program steps contain a dependency cycle"])
        resolved.update(ready)
        for step_id in ready:
            remaining.pop(step_id)

    result = dict(program)
    result["steps"] = normalized
    result.setdefault("output", "${steps}")
    return result


def _ensure_budget(program: Mapping[str, Any], value: Any, label: str) -> None:
    limit = int(settings.mcp_program_max_result_bytes)
    if _json_size(value) > limit:
        raise HTTPException(status_code=413, detail=f"MCP program {label} exceeds {limit} bytes")


async def _call_nested_tool(
    *, user_id: int, ai_config_id: Optional[int], tool: str, arguments: Dict[str, Any]
) -> Dict[str, Any]:
    if tool == PROGRAM_TOOL_NAME:
        raise HTTPException(status_code=400, detail="nested mcp.program calls are not allowed")
    if _json_size(arguments) > int(settings.mcp_program_max_argument_bytes):
        raise HTTPException(status_code=413, detail="nested MCP arguments exceed configured limit")

    from api.services.mcp.capability_view import ensure_tool_eligible
    from mcp_runtime.mcp.registry import registry

    if ai_config_id is not None:
        ensure_tool_eligible(user_id, ai_config_id, tool)
    if registry.has(tool):
        return await registry.call(tool, user_id, arguments, ai_config_id)

    # Device/workshop MCPs are not in the server registry. Reuse the existing
    # endpoint broker so the orchestrator does not create a second dispatch or
    # permission path. The broker routes to connector-runtime when this code is
    # running in the split mcp-runtime process.
    from connector_runtime.dispatch.desktop_device_tools import (
        is_endpoint_agent_tool,
        is_workshop_tool,
    )
    if is_endpoint_agent_tool(tool) or is_workshop_tool(tool):
        from ai_runtime.inference.tool_execution import call_mcp_or_endpoint_tool

        return await call_mcp_or_endpoint_tool(tool, user_id, arguments, ai_config_id)
    raise HTTPException(status_code=404, detail=f"Unknown nested MCP tool: {tool}")


async def _execute_step(
    step: Mapping[str, Any],
    context: Mapping[str, Any],
    *,
    user_id: int,
    ai_config_id: Optional[int],
    allowed_tools: Optional[set[str]],
    global_semaphore: asyncio.Semaphore,
) -> Dict[str, Any]:
    tool = str(step["tool"])
    if allowed_tools is not None and tool not in allowed_tools:
        raise HTTPException(status_code=403, detail=f"program tool not allowlisted: {tool}")
    foreach = step.get("foreach")
    if foreach is None:
        args = render_value(step["arguments"], context)
        async with global_semaphore:
            return await _call_nested_tool(
                user_id=user_id, ai_config_id=ai_config_id, tool=tool, arguments=args
            )

    items = render_value(foreach, context)
    if not isinstance(items, list):
        raise ProgramValidationError([f"step {step['id']}: foreach reference must resolve to an array"])
    if len(items) > int(settings.mcp_program_max_fanout):
        raise HTTPException(status_code=413, detail="MCP program foreach fan-out exceeds configured limit")
    semaphore = asyncio.Semaphore(int(step["concurrency"]))

    async def invoke(index: int, item: Any) -> Dict[str, Any]:
        local_context = dict(context)
        local_context["item"] = item
        local_context["index"] = index
        args = render_value(step["arguments"], local_context)
        async with semaphore:
            async with global_semaphore:
                try:
                    result = await _call_nested_tool(
                        user_id=user_id, ai_config_id=ai_config_id, tool=tool, arguments=args
                    )
                    return {"ok": True, "index": index, "item": item, "result": result}
                except Exception as exc:  # preserve per-item evidence for on_error=continue
                    if step["on_error"] == "continue":
                        return {"ok": False, "index": index, "item": item, "error": str(exc)}
                    raise

    return {"items": await asyncio.gather(*(invoke(i, item) for i, item in enumerate(items)))}


async def execute_program(
    program: Mapping[str, Any],
    *,
    user_id: int,
    ai_config_id: Optional[int],
    input_value: Optional[Dict[str, Any]] = None,
    allowed_tools: Optional[set[str]] = None,
    cancel_check: Optional[Callable[[], bool]] = None,
    program_id: Optional[str] = None,
) -> Dict[str, Any]:
    normalized = validate_program(program)
    started = time.monotonic()
    context: Dict[str, Any] = {"input": dict(input_value or {}), "steps": {}}
    global_semaphore = asyncio.Semaphore(int(settings.mcp_program_max_concurrency))
    pending = {step["id"]: step for step in normalized["steps"]}
    completed: set[str] = set()
    while pending:
        if cancel_check and cancel_check():
            raise ProgramCancelled()
        ready = [
            step for step in pending.values()
            if set(step["depends_on"]) <= completed
        ]
        if not ready:
            raise ProgramValidationError(["program could not make progress (dependency cycle)"])
        results = await asyncio.gather(
            *(
                _execute_step(
                    step,
                    context,
                    user_id=user_id,
                    ai_config_id=ai_config_id,
                    allowed_tools=allowed_tools,
                    global_semaphore=global_semaphore,
                )
                for step in ready
            ),
            return_exceptions=True,
        )
        fatal: Optional[BaseException] = None
        for step, result in zip(ready, results):
            if isinstance(result, BaseException):
                if step["on_error"] == "continue":
                    context["steps"][step["id"]] = {"ok": False, "error": str(result)}
                else:
                    fatal = result
                    break
            else:
                context["steps"][step["id"]] = result
            pending.pop(step["id"], None)
            completed.add(step["id"])
            _ensure_budget(normalized, context["steps"], "intermediate result")
        if fatal is not None:
            raise fatal
        if cancel_check and cancel_check():
            raise ProgramCancelled()
        if time.monotonic() - started > int(settings.mcp_program_max_runtime_seconds):
            raise HTTPException(status_code=504, detail="MCP program exceeded its runtime limit")

    output = render_value(normalized.get("output"), context)
    _ensure_budget(normalized, output, "output")
    return {
        "program_id": program_id or f"program_{uuid.uuid4().hex}",
        "status": "completed",
        "output": output,
        "steps": context["steps"],
        "elapsed_ms": round((time.monotonic() - started) * 1000, 2),
    }


async def _mcp_program(user_id: int, args: Dict[str, Any], ai_config_id: Optional[int]) -> Dict[str, Any]:
    if not settings.mcp_program_enabled:
        raise HTTPException(status_code=404, detail="MCP program orchestration is disabled")
    action = str(args.get("action") or "run").strip().lower()
    program = args.get("program")
    if action == "validate":
        return {"valid": True, "program": validate_program(program)}
    if action == "get":
        from mcp_runtime.mcp.program_jobs import get_program

        return get_program(
            user_id=user_id,
            ai_config_id=ai_config_id,
            run_id=str(args.get("run_id") or ""),
        )
    if action == "cancel":
        from mcp_runtime.mcp.program_jobs import cancel_program

        return cancel_program(
            user_id=user_id,
            ai_config_id=ai_config_id,
            run_id=str(args.get("run_id") or ""),
        )
    if action != "run":
        raise HTTPException(status_code=400, detail="mcp.program action must be validate, run, get or cancel")
    allowed = args.get("allowed_tools")
    allowlist = None
    if allowed is not None:
        if not isinstance(allowed, list) or not all(isinstance(item, str) for item in allowed):
            raise HTTPException(status_code=422, detail="allowed_tools must be a string array")
        allowlist = {item.strip() for item in allowed if item.strip()}
    input_value = args.get("input") if isinstance(args.get("input"), dict) else {}
    if _json_size(input_value) > int(settings.mcp_program_max_argument_bytes):
        raise HTTPException(status_code=413, detail="MCP program input exceeds configured limit")
    mode = str(args.get("mode") or "sync").strip().lower()
    if mode == "async":
        from mcp_runtime.mcp.program_jobs import enqueue_program

        return enqueue_program(
            user_id=user_id,
            ai_config_id=ai_config_id,
            program=program,
            input_value=input_value,
            allowed_tools=allowlist,
        )
    if mode != "sync":
        raise HTTPException(status_code=400, detail="mcp.program mode must be sync or async")
    return await execute_program(
        program,
        user_id=user_id,
        ai_config_id=ai_config_id,
        input_value=input_value,
        allowed_tools=allowlist,
    )


MCP_PROGRAM_SCHEMA = {
    "type": "object",
    "properties": {
        "action": {"type": "string", "enum": ["validate", "run", "get", "cancel"], "default": "run"},
        "program": {"type": "object"},
        "input": {"type": "object"},
        "mode": {"type": "string", "enum": ["sync", "async"], "default": "sync"},
        "run_id": {"type": "string"},
        "allowed_tools": {"type": "array", "items": {"type": "string"}, "maxItems": 100},
    },
    # ``program`` is required for validate/run and ``run_id`` is required for
    # get/cancel; the action-specific check lives in the handler so clients can
    # use one stable tool schema.
    "additionalProperties": False,
}
