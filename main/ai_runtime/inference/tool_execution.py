"""Execute one MCP or endpoint tool and normalize its outcome."""

from dataclasses import dataclass
import time
from typing import Any, Callable, Dict, Iterator, Optional

from api.chat_runtime.chat_prompt_utils import (
    _build_mcp_display_result,
    _extract_mcp_error,
)
from api.common.tool_outcomes import find_reported_failure, reported_failure_detail
from api.core.settings import settings
from api.runtime.async_bridge import run_async
from ai_runtime.inference.runtime_clients import (
    call_mcp_via_runtime,
    mcp_call_timeout,
    dispatch_endpoint_in_process,
    dispatch_endpoint_via_runtime,
    endpoint_dispatch_timeout,
)
from ai_runtime.inference.tool_resolution import joined_tool_skip_reason
from connector_runtime.dispatch.desktop_device_tools import (
    is_endpoint_agent_tool,
    is_workshop_tool,
)
from mcp_runtime.mcp import registry


@dataclass(frozen=True)
class ToolExecutionResult:
    result: Dict[str, object]
    failed: bool
    error: str
    display_text: str
    latency: float


@dataclass(frozen=True)
class JoinedToolRequest:
    tools: tuple[str, ...]
    arguments: dict
    allowed_tools: frozenset[str]
    user_id: int
    ai_config_id: Optional[int]


@dataclass(frozen=True)
class JoinedToolEvent:
    tool: str = ""
    execution: Optional[ToolExecutionResult] = None
    stopped: bool = False


async def call_mcp_or_endpoint_tool(
    tool: str,
    user_id: int,
    arguments: dict,
    ai_config_id: Optional[int],
) -> Dict[str, object]:
    # Server-owned names are reserved.  A device may accidentally advertise a
    # tool with the same name, but it must never shadow the toolbox contract.
    # Workshop tools are the only intentional endpoint-first namespace.
    server_owned = registry.has(tool)
    if is_workshop_tool(tool):
        return {
            "tool": tool,
            "destructive": True,
            "result": await dispatch_endpoint_in_process(
                user_id=user_id,
                ai_config_id=ai_config_id,
                tool=tool,
                arguments=arguments,
            ),
        }
    if not server_owned and is_endpoint_agent_tool(tool):
        # Bot-originated runs execute inside connector-runtime itself, which
        # owns the endpoint Socket.IO registry. Other split processes dispatch
        # through the configured socket owner.
        from api.devices.socket_owner import endpoint_dispatch_url

        agent_host_url = (
            ""
            if str(settings.service_role or "").strip().lower() == "connector"
            else endpoint_dispatch_url(
                settings.api_gateway_url,
                settings.connector_runtime_url,
            )
        )
        dispatch_timeout = endpoint_dispatch_timeout(tool, arguments)
        if agent_host_url:
            return {
                "tool": tool,
                "destructive": True,
                "result": await dispatch_endpoint_via_runtime(
                    agent_host_url,
                    tool,
                    user_id,
                    arguments,
                    ai_config_id,
                    timeout_seconds=dispatch_timeout,
                ),
            }
        return {
            "tool": tool,
            "destructive": True,
            "result": await dispatch_endpoint_in_process(
                user_id=user_id,
                ai_config_id=ai_config_id,
                tool=tool,
                arguments=arguments,
                timeout_seconds=dispatch_timeout,
            ),
        }

    # Search is a direct outbound call; keep it healthy when split MCP runtime
    # is unavailable instead of proxying it through 127.0.0.1:3001.
    if tool == "workspace.search":
        return await registry.call(tool, user_id, arguments, ai_config_id)

    if settings.mcp_runtime_url:
        return await call_mcp_via_runtime(
            settings.mcp_runtime_url,
            tool,
            user_id,
            arguments,
            ai_config_id,
        )
    return await registry.call(tool, user_id, arguments, ai_config_id)


def tool_result_failed(tool_result: Dict[str, object]) -> tuple[bool, str]:
    failure = find_reported_failure(tool_result)
    if failure is not None:
        return True, reported_failure_detail(failure)
    return False, ""


def execute_tool_call(
    tool: str,
    user_id: int,
    arguments: dict,
    ai_config_id: Optional[int],
) -> ToolExecutionResult:
    """Run one tool through the sync worker bridge with readable failures."""

    started_at = time.perf_counter()
    try:
        bridge_timeout = None
        if is_endpoint_agent_tool(tool):
            # Let the endpoint's inner result deadline win instead of the
            # async bridge's generic 120-second timeout.
            bridge_timeout = endpoint_dispatch_timeout(tool, arguments) + 30
        elif tool == "automation.manage" and str(arguments.get("action") or "").lower() in {"start", "run"}:
            bridge_timeout = mcp_call_timeout(tool, arguments) + 30
        result = run_async(
            call_mcp_or_endpoint_tool(tool, user_id, arguments, ai_config_id),
            timeout=bridge_timeout,
        )
        failed, error = tool_result_failed(result)
    except Exception as exc:
        failed = True
        error = _extract_mcp_error(exc)
        result = {"result": {"success": False, "error": error}}

    display_text = _build_mcp_display_result(
        tool,
        result,
        ok=not failed,
        error_message=error,
    )
    return ToolExecutionResult(
        result=result,
        failed=failed,
        error=error,
        display_text=display_text,
        latency=time.perf_counter() - started_at,
    )


def iter_joined_tool_executions(
    request: JoinedToolRequest,
    should_stop: Callable[[], bool],
    mark_waiting: Callable[[str, Dict[str, Any]], None],
) -> Iterator[JoinedToolEvent]:
    """Yield each joined-tool outcome in execution/persistence order."""

    for tool in request.tools:
        if should_stop():
            yield JoinedToolEvent(stopped=True)
            return
        started_at = time.perf_counter()
        error = joined_tool_skip_reason(
            tool,
            request.arguments,
            set(request.allowed_tools),
        )
        if error:
            result = {"result": {"success": False, "error": error}}
            execution = ToolExecutionResult(
                result=result,
                failed=True,
                error=error,
                display_text=_build_mcp_display_result(
                    tool,
                    result,
                    ok=False,
                    error_message=error,
                ),
                latency=time.perf_counter() - started_at,
            )
        else:
            mark_waiting(tool, request.arguments)
            execution = execute_tool_call(
                tool,
                request.user_id,
                request.arguments,
                request.ai_config_id,
            )
        yield JoinedToolEvent(tool=tool, execution=execution)
