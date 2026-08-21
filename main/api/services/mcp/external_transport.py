"""HTTP transport gates for the stateless external MCP endpoint."""

from contextlib import contextmanager
from collections import deque
import math
import threading
import time
from typing import Optional
from urllib.parse import urlsplit

from fastapi import Request
from fastapi.responses import JSONResponse

from api.core.settings import settings


MAX_REQUEST_BYTES = 1024 * 1024
MAX_CONCURRENT_CALLS_PER_CREDENTIAL = 2
MAX_CONCURRENT_CALLS_PER_MEMBER = 4
SUPPORTED_PROTOCOL_VERSIONS = {"2025-03-26", "2025-06-18"}
DEFAULT_PROTOCOL_VERSION = "2025-03-26"
RATE_WINDOW_SECONDS = 60
CONTROL_RATE_PER_CREDENTIAL = 120
CONTROL_RATE_PER_IP = 2400
CALL_RATE_PER_CREDENTIAL = 30
CALL_RATE_PER_IP = 600
DEFAULT_EXTERNAL_TOOL_TIMEOUT_SECONDS = 180
ENDPOINT_TIMEOUT_SAFETY_SECONDS = 30
MAX_ENDPOINT_EXTERNAL_TIMEOUT_SECONDS = 1830
# The direct peer may be a shared reverse proxy. IP buckets are deliberately
# broad abuse brakes; credential buckets remain the primary rate boundary.
_CALL_COUNTS: dict[tuple[str, int], int] = {}
_CALL_COUNTS_LOCK = threading.Lock()
_RATE_EVENTS: dict[tuple[str, str, str], deque[float]] = {}
_RATE_LOCK = threading.Lock()


def validate_transport_headers(request: Request) -> Optional[JSONResponse]:
    media_type = str(request.headers.get("content-type") or "").split(";", 1)[0].strip().lower()
    if media_type != "application/json":
        return JSONResponse(status_code=415, content={"detail": "Content-Type must be application/json"})
    accepted_types = {
        item.split(";", 1)[0].strip().lower()
        for item in str(request.headers.get("accept") or "").split(",")
    }
    required_accept = {"application/json", "text/event-stream"}
    if not required_accept.issubset(accepted_types):
        return JSONResponse(
            status_code=406,
            content={"detail": "Accept must include application/json and text/event-stream"},
        )
    origin = str(request.headers.get("origin") or "").strip()
    if origin and origin not in _allowed_origins(request):
        return JSONResponse(status_code=403, content={"detail": "Origin is not allowed"})
    return None


def validate_protocol_version(request: Request, message: dict) -> Optional[JSONResponse]:
    if message.get("method") == "initialize":
        return None
    version = (
        str(request.headers.get("mcp-protocol-version") or "").strip()
        or DEFAULT_PROTOCOL_VERSION
    )
    if version not in SUPPORTED_PROTOCOL_VERSIONS:
        return JSONResponse(status_code=400, content={"detail": "Unsupported MCP-Protocol-Version"})
    return None


def rate_limit_ip(request: Request, method: str) -> Optional[JSONResponse]:
    peer_ip = str(getattr(request.client, "host", "") or "unknown")
    bucket = _method_bucket(method)
    limit = CALL_RATE_PER_IP if bucket == "call" else CONTROL_RATE_PER_IP
    return _rate_limit(("ip", peer_ip, bucket), limit)


def rate_limit_credential(credential_id: int, method: str) -> Optional[JSONResponse]:
    bucket = _method_bucket(method)
    limit = (
        CALL_RATE_PER_CREDENTIAL
        if bucket == "call"
        else CONTROL_RATE_PER_CREDENTIAL
    )
    return _rate_limit(("credential", str(credential_id), bucket), limit)


def _method_bucket(method: str) -> str:
    return "call" if str(method) == "tools/call" else "control"


def _rate_limit(key: tuple[str, str, str], limit: int) -> Optional[JSONResponse]:
    now = time.monotonic()
    cutoff = now - RATE_WINDOW_SECONDS
    with _RATE_LOCK:
        events = _RATE_EVENTS.setdefault(key, deque())
        while events and events[0] <= cutoff:
            events.popleft()
        if len(events) >= max(1, int(limit)):
            retry_after = max(1, math.ceil(RATE_WINDOW_SECONDS - (now - events[0])))
            return JSONResponse(
                status_code=429,
                content={"detail": "External MCP rate limit exceeded"},
                headers={"Retry-After": str(retry_after)},
            )
        events.append(now)
        if len(_RATE_EVENTS) > 4096:
            _prune_rate_events(cutoff)
    return None


def _prune_rate_events(cutoff: float) -> None:
    for key, events in tuple(_RATE_EVENTS.items()):
        while events and events[0] <= cutoff:
            events.popleft()
        if not events:
            _RATE_EVENTS.pop(key, None)


def external_tool_timeout(tool: str, arguments: object) -> int:
    from connector_runtime.dispatch.desktop_device_tools import is_endpoint_agent_tool

    args = arguments if isinstance(arguments, dict) else {}
    if is_endpoint_agent_tool(tool):
        from ai_runtime.inference.runtime_clients import endpoint_dispatch_timeout

        return endpoint_dispatch_timeout(tool, args) + ENDPOINT_TIMEOUT_SAFETY_SECONDS
    action = str(args.get("action") or "").strip().lower()
    if tool == "automation.manage" and action in {"start", "run"}:
        from ai_runtime.inference.runtime_clients import mcp_call_timeout

        return int(mcp_call_timeout(tool, args)) + ENDPOINT_TIMEOUT_SAFETY_SECONDS
    return DEFAULT_EXTERNAL_TOOL_TIMEOUT_SECONDS


def codex_tool_timeout_seconds() -> int:
    # Codex must outlive the gateway's own deadline so transport overhead does
    # not make the client abandon a call while the server is still finalizing it.
    workflow_timeout = int(settings.workflow_chat_wait_timeout_seconds) + 360
    return max(MAX_ENDPOINT_EXTERNAL_TIMEOUT_SECONDS + 30, workflow_timeout)


def _allowed_origins(request: Request) -> set[str]:
    origins = {
        _url_origin(str(settings.public_base_url or "")),
    }
    request_parts = urlsplit(str(request.base_url))
    port = f":{request_parts.port}" if request_parts.port else ""
    scheme = request_parts.scheme if request_parts.scheme in {"http", "https"} else "http"
    origins.update({
        f"{scheme}://localhost{port}",
        f"{scheme}://127.0.0.1{port}",
        f"{scheme}://[::1]{port}",
    })
    if request_parts.hostname in {"localhost", "127.0.0.1", "::1"}:
        origins.add(_url_origin(str(request.base_url)))
    return {item for item in origins if item}


def _url_origin(value: str) -> str:
    parts = urlsplit(str(value or "").strip())
    if parts.scheme not in {"http", "https"} or not parts.netloc:
        return ""
    return f"{parts.scheme}://{parts.netloc}"


@contextmanager
def external_call_slot(credential_id: int, ai_config_id: int):
    acquired = False
    credential_key = ("credential", int(credential_id))
    member_key = ("member", int(ai_config_id))
    with _CALL_COUNTS_LOCK:
        credential_active = _CALL_COUNTS.get(credential_key, 0)
        member_active = _CALL_COUNTS.get(member_key, 0)
        if (
            credential_active < MAX_CONCURRENT_CALLS_PER_CREDENTIAL
            and member_active < MAX_CONCURRENT_CALLS_PER_MEMBER
        ):
            _CALL_COUNTS[credential_key] = credential_active + 1
            _CALL_COUNTS[member_key] = member_active + 1
            acquired = True
    try:
        yield acquired
    finally:
        if acquired:
            with _CALL_COUNTS_LOCK:
                _release_call_count(credential_key)
                _release_call_count(member_key)


def _release_call_count(key: tuple[str, int]) -> None:
    remaining = _CALL_COUNTS.get(key, 1) - 1
    if remaining > 0:
        _CALL_COUNTS[key] = remaining
    else:
        _CALL_COUNTS.pop(key, None)
