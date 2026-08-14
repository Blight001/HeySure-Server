"""Bounded cross-runtime MCP and endpoint-dispatch clients."""

import asyncio
from typing import Any, Dict, Optional

import httpx

from api.core.settings import settings
from api.runtime.internal_http import internal_headers, internal_post
from api.runtime.run_context import get_run_session_context
from connector_runtime.dispatch.device_dispatch import dispatch_endpoint_tool_and_wait


LONG_RUN_ENDPOINT_DEFAULT_TIMEOUT = 900
ENDPOINT_RESULT_DELIVERY_GRACE = 120
ENDPOINT_DISPATCH_TIMEOUT_CAP = 1800
WORKFLOW_CHAT_WAIT_GRACE_SECONDS = 300


def mcp_call_timeout(tool: str, arguments: dict) -> float:
    action = str((arguments or {}).get("action") or "").strip().lower()
    if tool == "automation.manage" and action in {"start", "run"}:
        return float(settings.workflow_chat_wait_timeout_seconds + WORKFLOW_CHAT_WAIT_GRACE_SECONDS)
    return 120.0


async def call_mcp_via_runtime(
    runtime_url: str,
    tool: str,
    user_id: int,
    arguments: dict,
    ai_config_id: Optional[int],
) -> Dict[str, object]:
    body = {
        "tool": tool,
        "user_id": user_id,
        "ai_config_id": ai_config_id,
        "arguments": arguments,
    }
    run_ctx = get_run_session_context()
    if run_ctx:
        body["session_context"] = run_ctx
    return await internal_post(
        runtime_url,
        "/internal/mcp/call",
        json=body,
        timeout=mcp_call_timeout(tool, arguments),
    )


async def dispatch_endpoint_in_process(
    *,
    user_id: int,
    ai_config_id: Optional[int],
    tool: str,
    arguments: dict,
    timeout_seconds: Optional[int] = None,
) -> Dict[str, object]:
    """Dispatch through the socket-owning process without an HTTP hop."""

    kwargs: dict[str, object] = {
        "user_id": user_id,
        "ai_config_id": ai_config_id,
        "tool": tool,
        "args": arguments,
    }
    if timeout_seconds is not None:
        kwargs["timeout_seconds"] = timeout_seconds
    return await dispatch_endpoint_tool_and_wait(**kwargs)


async def dispatch_endpoint_via_runtime(
    runtime_url: str,
    tool: str,
    user_id: int,
    arguments: dict,
    ai_config_id: Optional[int],
    timeout_seconds: int = 120,
    poll_interval: float = 0.25,
) -> Dict[str, object]:
    headers = internal_headers()
    async with httpx.AsyncClient(base_url=runtime_url.rstrip("/"), timeout=30.0) as client:
        response = await client.post(
            "/internal/agent/dispatch",
            headers=headers,
            json={
                "user_id": user_id,
                "ai_config_id": ai_config_id,
                "tool": tool,
                "arguments": arguments,
            },
        )
        response.raise_for_status()
        task_id = str(response.json().get("task_id") or "")
        if not task_id:
            return {"success": False, "tool": tool, "error": "connector-runtime returned no task_id"}

        deadline = asyncio.get_running_loop().time() + max(1, int(timeout_seconds))
        consecutive_missing = 0
        while True:
            row, consecutive_missing = await _read_dispatch_row(
                client, task_id, headers, consecutive_missing
            )
            status = str(row.get("status") or "pending")
            if status not in {"pending", "queued"}:
                return {
                    "success": bool(row.get("success", status == "completed")),
                    "taskId": task_id,
                    "deviceId": row.get("device_id") or row.get("deviceId"),
                    "tool": row.get("tool") or tool,
                    "summary": row.get("summary"),
                    "result": row.get("result"),
                    "error": row.get("error"),
                }
            if asyncio.get_running_loop().time() >= deadline:
                await _expire_dispatch(client, task_id, headers, timeout_seconds)
                return {
                    "success": False,
                    "taskId": task_id,
                    "tool": tool,
                    "error": f"Endpoint agent result timeout after {timeout_seconds}s",
                }
            await asyncio.sleep(poll_interval)


async def _read_dispatch_row(client, task_id, headers, consecutive_missing):
    try:
        response = await client.get(
            f"/internal/agent/dispatch/result/{task_id}", headers=headers
        )
        if response.status_code != 404:
            response.raise_for_status()
            return response.json(), 0
        consecutive_missing += 1
        if consecutive_missing >= 3:
            return {
                "status": "error",
                "success": False,
                "error": "dispatch row missing after retries (connector-runtime restart?)",
            }, consecutive_missing
        return {"status": "pending"}, consecutive_missing
    except Exception as exc:
        return {"status": "pending", "error": f"poll error: {exc}"}, consecutive_missing


async def _expire_dispatch(client, task_id, headers, timeout_seconds):
    try:
        await client.post(
            f"/internal/agent/dispatch/expire/{task_id}",
            headers=headers,
            json={"reason": f"Endpoint agent result timeout after {timeout_seconds}s"},
        )
    except Exception:
        pass


def endpoint_dispatch_timeout(tool: str, arguments: dict) -> int:
    args = arguments if isinstance(arguments, dict) else {}
    raw = args.get("timeout_seconds")
    if raw is not None:
        try:
            execution_timeout = max(5, int(raw))
            return min(
                execution_timeout + ENDPOINT_RESULT_DELIVERY_GRACE,
                ENDPOINT_DISPATCH_TIMEOUT_CAP,
            )
        except (TypeError, ValueError):
            pass
    action = str(args.get("action") or "").strip().lower()
    if (tool == "manage_card" and action in {"run", "execute"}) or tool == "run_card":
        return LONG_RUN_ENDPOINT_DEFAULT_TIMEOUT
    return 120
