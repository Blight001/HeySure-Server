"""Stateless Streamable HTTP MCP transport for one externally shared member."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
import json
import time
from typing import Any, Optional

from fastapi import APIRouter, Header, HTTPException, Request
from fastapi.encoders import jsonable_encoder
from fastapi.responses import JSONResponse, Response
from sqlmodel import Session

from api.database import engine
from api.services.mcp import mcp_stats
from api.services.mcp.external_access import (
    ExternalMcpAccessError,
    ExternalMcpPrincipal,
    authenticate_credential,
    ensure_member_available,
    is_known_external_tool,
    record_audit,
)
from api.services.mcp.external_transport import (
    MAX_REQUEST_BYTES as _MAX_REQUEST_BYTES,
    SUPPORTED_PROTOCOL_VERSIONS as _SUPPORTED_PROTOCOL_VERSIONS,
    external_call_slot as _external_call_slot,
    external_tool_timeout,
    rate_limit_credential,
    rate_limit_ip,
    validate_protocol_version as _validate_protocol_version,
    validate_transport_headers as _validate_transport_headers,
)
from ai_runtime.inference.tool_execution import (
    call_mcp_or_endpoint_tool,
    tool_result_failed,
)


router = APIRouter()
PREFIX = ""
_LATEST_PROTOCOL_VERSION = "2025-06-18"
_REQUEST_METHODS = {"initialize", "ping", "tools/list", "tools/call"}


@dataclass(frozen=True)
class RpcFault(Exception):
    code: int
    message: str
    data: Optional[dict] = None
    audit_code: str = "rpc_error"
    tool_name: str = ""


@dataclass(frozen=True)
class DispatchResult:
    payload: dict
    success: bool = True
    error_code: str = ""
    tool_name: str = ""


@router.post("/mcp/member")
async def canonical_member_mcp(
    request: Request,
    authorization: Optional[str] = Header(None),
):
    return await _handle_request(request, authorization, public_id=None)


@router.post("/mcp/members/{public_id}")
async def identified_member_mcp(
    public_id: str,
    request: Request,
    authorization: Optional[str] = Header(None),
):
    return await _handle_request(request, authorization, public_id=public_id)


async def _handle_request(request: Request, authorization: Optional[str], public_id: Optional[str]):
    transport_error = _validate_transport_headers(request)
    if transport_error is not None:
        return transport_error
    message, invalid = await _read_message(request)
    if invalid is not None:
        return invalid
    rate_error = rate_limit_ip(request, str(message["method"]))
    if rate_error is not None:
        return rate_error
    access_error = None
    with Session(engine) as session:
        try:
            principal, cfg = authenticate_credential(
                session,
                authorization,
                public_id=public_id,
            )
        except ExternalMcpAccessError as exc:
            return JSONResponse(
                status_code=exc.http_status,
                content={"detail": str(exc)},
                headers={"WWW-Authenticate": "Bearer"} if exc.http_status == 401 else {},
            )
        try:
            ensure_member_available(cfg)
        except ExternalMcpAccessError as exc:
            access_error = exc
    version_error = _validate_protocol_version(request, message)
    if version_error is not None:
        return version_error
    rate_error = rate_limit_credential(principal.credential_id, str(message["method"]))
    if rate_error is not None:
        record_audit(
            principal,
            method=str(message["method"]),
            success=False,
            error_code="rate_limited",
        )
        return rate_error
    if access_error is not None:
        fault = RpcFault(-32001, str(access_error), audit_code=access_error.code)
        _audit_fault(principal, str(message["method"]), fault, time.perf_counter())
        if "id" not in message:
            return Response(status_code=202)
        return _error_response(message.get("id"), fault)
    return await _dispatch_and_respond(principal, message)


async def _read_message(request: Request) -> tuple[Optional[dict], Optional[JSONResponse]]:
    content_length = str(request.headers.get("content-length") or "").strip()
    if content_length.isdigit() and int(content_length) > _MAX_REQUEST_BYTES:
        return None, JSONResponse(status_code=413, content={"detail": "Request body too large"})
    try:
        body = bytearray()
        async for chunk in request.stream():
            body.extend(chunk)
            if len(body) > _MAX_REQUEST_BYTES:
                return None, JSONResponse(status_code=413, content={"detail": "Request body too large"})
        message = json.loads(body)
    except Exception:
        return None, _error_response(None, RpcFault(-32700, "Parse error"), status_code=400)
    if not isinstance(message, dict):
        return None, _error_response(None, RpcFault(-32600, "Invalid Request"), status_code=400)
    if message.get("jsonrpc") != "2.0" or not isinstance(message.get("method"), str):
        return None, _error_response(message.get("id"), RpcFault(-32600, "Invalid Request"), status_code=400)
    return message, None


async def _dispatch_and_respond(principal, message: dict):
    request_id = message.get("id")
    notification = "id" not in message
    method = str(message["method"])
    started = time.perf_counter()
    semantic_fault = _validate_message_semantics(message)
    if semantic_fault is not None:
        _audit_fault(principal, method, semantic_fault, started)
        return (
            Response(status_code=202)
            if notification
            else _error_response(request_id, semantic_fault)
        )
    try:
        if method == "tools/call":
            with _external_call_slot(
                principal.credential_id,
                principal.ai_config_id,
            ) as acquired:
                if not acquired:
                    raise RpcFault(
                        -32002,
                        "Too many concurrent tool calls",
                        audit_code="concurrency_limited",
                        tool_name=_requested_tool(message.get("params")),
                    )
                outcome = await _dispatch_tool_call_with_timeout(
                    principal,
                    message.get("params"),
                )
        else:
            outcome = await _dispatch(method, message.get("params"), principal)
    except RpcFault as fault:
        _audit_fault(principal, method, fault, started)
        return Response(status_code=202) if notification else _error_response(request_id, fault)
    except ExternalMcpAccessError as exc:
        fault = RpcFault(-32001, str(exc), audit_code=exc.code)
        _audit_fault(principal, method, fault, started)
        return Response(status_code=202) if notification else _error_response(request_id, fault)
    except Exception:
        fault = RpcFault(-32603, "Internal error", audit_code="internal_error")
        _audit_fault(principal, method, fault, started)
        return Response(status_code=202) if notification else _error_response(request_id, fault)
    record_audit(
        principal,
        method=method,
        tool_name=outcome.tool_name,
        success=outcome.success,
        error_code=outcome.error_code,
        duration_ms=_elapsed_ms(started),
    )
    if notification:
        return Response(status_code=202)
    return JSONResponse({"jsonrpc": "2.0", "id": request_id, "result": outcome.payload})


async def _dispatch(method: str, params: Any, principal) -> DispatchResult:
    if method == "initialize":
        return DispatchResult(_initialize_result(params))
    if method == "ping":
        return DispatchResult({})
    if method == "notifications/initialized":
        return DispatchResult({})
    if method == "tools/list":
        return DispatchResult(_tools_list(principal))
    if method == "tools/call":
        return await _tools_call(principal, params)
    raise RpcFault(-32601, "Method not found", audit_code="method_not_found")


def _initialize_result(params: Any) -> dict:
    if not isinstance(params, dict) or not _nonempty_string(params.get("protocolVersion")):
        raise RpcFault(-32602, "Invalid params", audit_code="invalid_params")
    capabilities = params.get("capabilities")
    client_info = params.get("clientInfo")
    if not isinstance(capabilities, dict) or not isinstance(client_info, dict):
        raise RpcFault(-32602, "Invalid params", audit_code="invalid_params")
    if not _nonempty_string(client_info.get("name")) or not _nonempty_string(client_info.get("version")):
        raise RpcFault(-32602, "Invalid params", audit_code="invalid_params")
    requested = str(params.get("protocolVersion") or "")
    version = requested if requested in _SUPPORTED_PROTOCOL_VERSIONS else _LATEST_PROTOCOL_VERSION
    return {
        "protocolVersion": version,
        "capabilities": {"tools": {"listChanged": False}},
        "serverInfo": {"name": "HeySure Member MCP", "version": "2.0"},
        "instructions": "Tools are the currently authorized capabilities of one HeySure digital member.",
    }


def _tools_list(principal: ExternalMcpPrincipal) -> dict:
    from api.services.mcp.capability_view import scoped_tool_view_for_ids

    view = scoped_tool_view_for_ids(principal.user_id, principal.ai_config_id)
    tools = []
    for name, capability in view.eligible.items():
        destructive = bool(capability.destructive)
        input_schema = dict(capability.input_schema)
        input_schema["type"] = "object"
        tools.append({
            "name": name,
            "description": capability.description,
            "inputSchema": input_schema,
            "annotations": {
                "destructiveHint": destructive,
                "readOnlyHint": not destructive,
            },
        })
    return {"tools": tools}


async def _tools_call(principal: ExternalMcpPrincipal, params: Any) -> DispatchResult:
    if not isinstance(params, dict):
        raise RpcFault(-32602, "Invalid params", audit_code="invalid_params")
    tool = str(params.get("name") or "").strip()
    arguments = params.get("arguments", {})
    if not tool or not isinstance(arguments, dict):
        raise RpcFault(-32602, "Invalid params", audit_code="invalid_params", tool_name=tool)
    try:
        from api.services.mcp.capability_view import ensure_tool_eligible

        ensure_tool_eligible(principal.user_id, principal.ai_config_id, tool)
    except HTTPException as exc:
        if exc.status_code == 404:
            return _unavailable_tool_result(principal, tool)
        if exc.status_code != 403:
            raise
        from api.services.mcp.capability_view import scoped_tool_view_for_ids

        view = scoped_tool_view_for_ids(principal.user_id, principal.ai_config_id)
        if not is_known_external_tool(principal.user_id, tool, view):
            raise RpcFault(
                -32602,
                "Unknown tool",
                audit_code="unknown_tool",
                tool_name=tool,
            ) from exc
        return _unavailable_tool_result(principal, tool)
    try:
        result = await call_mcp_or_endpoint_tool(
            tool,
            principal.user_id,
            arguments,
            principal.ai_config_id,
        )
        failed, error = tool_result_failed(result)
    except Exception as exc:
        # A tool can intentionally reject caller-supplied input (for example a
        # workflow compiler returning HTTP 422).  In split-runtime mode this
        # arrives here as an HTTP client exception.  Preserve safe 4xx details
        # as an MCP tool result so clients can repair the request instead of
        # seeing the misleading JSON-RPC "Internal error".
        result = _request_rejection_result(tool, exc)
        if result is None:
            _record_mcp_stat(principal, tool, False, "tool_execution_error")
            raise RpcFault(
                -32603,
                "Internal error",
                audit_code="tool_execution_error",
                tool_name=tool,
            ) from exc
        failed, error = tool_result_failed(result)
        _record_mcp_stat(principal, tool, False, "tool_rejected")
        return DispatchResult(
            _tool_result_payload(result, failed, error),
            success=False,
            error_code="tool_rejected",
            tool_name=tool,
        )
    _record_mcp_stat(
        principal,
        tool,
        not failed,
        "tool_reported_failure" if failed else "",
    )
    payload = _tool_result_payload(result, failed, error)
    return DispatchResult(
        payload,
        success=not failed,
        error_code="tool_failed" if failed else "",
        tool_name=tool,
    )


def _request_rejection_result(tool: str, exc: Exception) -> Optional[dict]:
    """Convert safe client-facing 4xx tool rejections into a normal MCP result."""

    status_code: Optional[int] = None
    detail: Any = None
    if isinstance(exc, HTTPException):
        status_code = exc.status_code
        detail = exc.detail
    else:
        response = getattr(exc, "response", None)
        raw_status = getattr(response, "status_code", None)
        if isinstance(raw_status, int):
            status_code = raw_status
            try:
                payload = response.json()
            except Exception:
                payload = None
            if isinstance(payload, dict):
                detail = payload.get("detail")

    # Server faults must remain opaque; only ordinary client-side request
    # rejections are safe and actionable to expose through external MCP.
    if not isinstance(status_code, int) or not 400 <= status_code < 500:
        return None

    safe_detail = _safe_request_rejection_detail(detail)
    return {
        "tool": tool,
        "destructive": False,
        "result": {
            "success": False,
            "failure_type": "request_rejected",
            "status_code": status_code,
            "error": _request_rejection_message(safe_detail, status_code),
            "detail": safe_detail,
        },
    }


def _safe_request_rejection_detail(detail: Any) -> Any:
    """Keep a bounded JSON-safe detail payload without exposing server traces."""

    try:
        encoded = jsonable_encoder(detail)
        encoded_json = json.dumps(encoded, ensure_ascii=False, separators=(",", ":"))
    except (TypeError, ValueError):
        return "Request rejected"
    if len(encoded_json) > 8_000:
        return "Request rejected; response detail was too large"
    return encoded


def _request_rejection_message(detail: Any, status_code: int) -> str:
    if isinstance(detail, str) and detail.strip():
        return detail.strip()
    if isinstance(detail, dict):
        errors = detail.get("errors")
        if isinstance(errors, list) and errors:
            text = "; ".join(str(item) for item in errors if str(item).strip())
            if text:
                return text[:4_000]
        for key in ("message", "error", "code"):
            value = detail.get(key)
            if isinstance(value, str) and value.strip():
                return value.strip()
    if isinstance(detail, list):
        text = "; ".join(str(item) for item in detail if str(item).strip())
        if text:
            return text[:4_000]
    return f"Tool request rejected (HTTP {status_code})"


def _tool_result_payload(result: Any, failed: bool, error: str) -> dict:
    encoded = jsonable_encoder(result)
    try:
        text = json.dumps(encoded, ensure_ascii=False, separators=(",", ":"))
    except (TypeError, ValueError):
        encoded = {"success": False, "error": "Tool result is not serializable"}
        text = json.dumps(encoded, ensure_ascii=False)
        failed = True
    payload = {"content": [{"type": "text", "text": text}], "isError": bool(failed)}
    if isinstance(encoded, dict):
        payload["structuredContent"] = encoded
    if failed and error and not text:
        payload["content"] = [{"type": "text", "text": error}]
    return payload


def _record_mcp_stat(principal, tool: str, success: bool, error: str) -> None:
    mcp_stats.record_call(
        user_id=principal.user_id,
        ai_config_id=principal.ai_config_id,
        tool=tool,
        success=success,
        error=error,
    )


def _audit_fault(principal, method: str, fault: RpcFault, started: float) -> None:
    record_audit(
        principal,
        method=method,
        tool_name=fault.tool_name,
        success=False,
        error_code=fault.audit_code,
        duration_ms=_elapsed_ms(started),
    )


def _elapsed_ms(started: float) -> int:
    return max(0, round((time.perf_counter() - started) * 1000))


def _error_response(request_id: Any, fault: RpcFault, *, status_code: int = 200) -> JSONResponse:
    error = {"code": fault.code, "message": fault.message}
    if fault.data is not None:
        error["data"] = fault.data
    return JSONResponse(
        status_code=status_code,
        content={"jsonrpc": "2.0", "id": request_id, "error": error},
    )


async def _dispatch_tool_call_with_timeout(principal, params: Any) -> DispatchResult:
    tool = _requested_tool(params)
    arguments = params.get("arguments", {}) if isinstance(params, dict) else {}
    try:
        return await asyncio.wait_for(
            _tools_call(principal, params),
            timeout=external_tool_timeout(tool, arguments),
        )
    except asyncio.TimeoutError:
        _record_mcp_stat(principal, tool, False, "external_tool_timeout")
        result = {"result": {"success": False, "error": "Tool call timed out"}}
        return DispatchResult(
            _tool_result_payload(result, True, "Tool call timed out"),
            success=False,
            error_code="tool_timeout",
            tool_name=tool,
        )


def _requested_tool(params: Any) -> str:
    return str(params.get("name") or "").strip() if isinstance(params, dict) else ""


def _validate_message_semantics(message: dict) -> Optional[RpcFault]:
    method = str(message["method"])
    has_id = "id" in message
    request_id = message.get("id")
    if has_id and not _valid_request_id(request_id, True):
        return RpcFault(-32600, "Invalid Request", audit_code="invalid_request")
    if method == "notifications/initialized":
        if has_id:
            return RpcFault(-32600, "Invalid Request", audit_code="invalid_request")
        if message.get("params") is not None and not isinstance(message.get("params"), dict):
            return RpcFault(-32602, "Invalid params", audit_code="invalid_params")
        return None
    if method.startswith("notifications/") and has_id:
        return RpcFault(-32600, "Invalid Request", audit_code="invalid_request")
    if method in _REQUEST_METHODS and not has_id:
        return RpcFault(-32600, "Invalid Request", audit_code="invalid_request")
    if method in {"ping", "tools/list"}:
        params = message.get("params")
        if params is not None and not isinstance(params, dict):
            return RpcFault(-32602, "Invalid params", audit_code="invalid_params")
    return None


def _valid_request_id(value: Any, present: bool) -> bool:
    return present and not isinstance(value, bool) and isinstance(value, (str, int))


def _nonempty_string(value: Any) -> bool:
    return isinstance(value, str) and bool(value.strip())


def _unavailable_tool_result(principal, tool: str) -> DispatchResult:
    _record_mcp_stat(principal, tool, False, "tool_unavailable")
    result = {"result": {"success": False, "error": "Tool is currently unavailable"}}
    return DispatchResult(
        _tool_result_payload(result, True, "Tool is currently unavailable"),
        success=False,
        error_code="tool_unavailable",
        tool_name=tool,
    )
