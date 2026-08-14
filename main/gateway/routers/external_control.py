"""User-facing setup APIs and the public Streamable HTTP MCP endpoint."""

import json
import time
from typing import Any, Dict, Optional

from fastapi import APIRouter, Depends, Header, HTTPException, Request, Response
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field
from sqlmodel import Session, select

from api.core.settings import settings
from api.database import get_session
from api.auth import create_access_token
from api.models.external_control import ExternalControllerCredential, ExternalControllerRun
from api.services.external_control import ExternalControlService
from mcp_runtime.mcp import registry

from .auth import get_current_user
from .mcp import MCPCallRequest, call_mcp_tool


router = APIRouter()
PREFIX = ""
PROTOCOL_VERSION = "2025-03-26"


class IssueCredentialRequest(BaseModel):
    label: str = Field(default="Codex", max_length=80)
    ttl_days: int = Field(default=30, ge=1, le=90)


def _public_base(request: Request) -> str:
    configured = str(settings.public_base_url or "").strip().rstrip("/")
    if configured:
        return configured
    forwarded_host = str(request.headers.get("x-forwarded-host") or "").split(",", 1)[0].strip()
    forwarded_proto = str(request.headers.get("x-forwarded-proto") or "").split(",", 1)[0].strip().lower()
    if forwarded_host and not any(char.isspace() or char in "/\\" for char in forwarded_host):
        scheme = forwarded_proto if forwarded_proto in {"http", "https"} else request.url.scheme
        return f"{scheme}://{forwarded_host}"
    return str(request.base_url).rstrip("/")


def _credential_payload(row: ExternalControllerCredential) -> dict:
    return {
        "id": row.id,
        "label": row.label,
        "token_prefix": row.token_prefix,
        "state": row.state,
        "created_at": row.created_at,
        "expires_at": row.expires_at,
        "last_seen_at": row.last_seen_at,
        "revoked_at": row.revoked_at,
    }


def _run_payload(row: ExternalControllerRun) -> dict:
    """Serialize the stable controller-run contract explicitly.

    Freshly committed SQLModel instances can serialize as an empty mapping on
    some SQLModel/Pydantic combinations.  The MCP response must never lose the
    run identifier that the controller needs for subsequent tool calls.
    """
    return {
        "run_id": row.run_id,
        "user_id": row.user_id,
        "ai_config_id": row.ai_config_id,
        "credential_id": row.credential_id,
        "status": row.status,
        "title": row.title,
        "summary": row.summary,
        "error_message": row.error_message,
        "lease_owner": row.lease_owner,
        "lease_expires_at": row.lease_expires_at,
        "created_at": row.created_at,
        "updated_at": row.updated_at,
        "started_at": row.started_at,
        "finished_at": row.finished_at,
    }


def _handoff_markdown(member_name: str, ai_config_id: int, endpoint: str, token: str) -> str:
    env_name = f"HEYSURE_CONTROLLER_TOKEN_{ai_config_id}"
    server_name = f"heysure_member_{ai_config_id}"
    return f"""# HeySure 外部 MCP 控制交接

你将作为 HeySure 数字成员「{member_name}」的外部控制器。连接后先调用 `heysure.get_context`，读取当前 Prompt、绑定设备与权限；所有服务器或设备动作必须调用 `heysure.call_mcp`，不得猜测执行结果。开始一段工作时调用 `heysure.start_run`，结束时调用 `heysure.finish_run`。

MCP 地址：`{endpoint}`

一次性显示的 Bearer Token：`{token}`

Codex 配置（将 Token 放入环境变量，不要写进仓库）：

```powershell
$env:{env_name}='{token}'
codex mcp add {server_name} --url {endpoint} --bearer-token-env-var {env_name}
```

也可以加入 `~/.codex/config.toml`：

```toml
[mcp_servers.{server_name}]
url = "{endpoint}"
bearer_token_env_var = "{env_name}"
```

配置后重启 Codex。该凭证仅绑定数字成员 ID {ai_config_id}，可随时在 HeySure 中吊销。
"""


@router.post("/api/external-control/{ai_config_id}/credentials")
def issue_controller_credential(
    ai_config_id: int,
    body: IssueCredentialRequest,
    request: Request,
    session: Session = Depends(get_session),
    authorization: str = Header(None),
):
    user = get_current_user(authorization, session)
    service = ExternalControlService(session)
    cfg = service.get_member(user.id, ai_config_id)
    credential, token = service.issue_credential(user.id, ai_config_id, body.label, body.ttl_days)
    endpoint = f"{_public_base(request)}/mcp/external"
    return {
        "credential": _credential_payload(credential),
        "endpoint": endpoint,
        "token": token,
        "handoff_markdown": _handoff_markdown(cfg.name, ai_config_id, endpoint, token),
        "warning": "Token is shown only in this response. Store it in an environment variable.",
    }


@router.get("/api/external-control/{ai_config_id}")
def controller_status(
    ai_config_id: int,
    session: Session = Depends(get_session),
    authorization: str = Header(None),
):
    user = get_current_user(authorization, session)
    service = ExternalControlService(session)
    cfg = service.get_member(user.id, ai_config_id)
    credentials = session.exec(
        select(ExternalControllerCredential).where(
            ExternalControllerCredential.user_id == user.id,
            ExternalControllerCredential.ai_config_id == ai_config_id,
        ).order_by(ExternalControllerCredential.created_at.desc()).limit(20)
    ).all()
    runs = session.exec(
        select(ExternalControllerRun).where(
            ExternalControllerRun.user_id == user.id,
            ExternalControllerRun.ai_config_id == ai_config_id,
        ).order_by(ExternalControllerRun.created_at.desc()).limit(20)
    ).all()
    return {
        "ai_config_id": ai_config_id,
        "execution_mode": cfg.execution_mode,
        "credentials": [_credential_payload(row) for row in credentials],
        "runs": [_run_payload(row) for row in runs],
        "events": service.list_events(user.id, ai_config_id, 100),
    }


@router.delete("/api/external-control/{ai_config_id}/credentials/{credential_id}")
def revoke_controller_credential(
    ai_config_id: int,
    credential_id: int,
    session: Session = Depends(get_session),
    authorization: str = Header(None),
):
    user = get_current_user(authorization, session)
    service = ExternalControlService(session)
    service.get_member(user.id, ai_config_id)
    return {"revoked": service.revoke(user.id, ai_config_id, credential_id)}


def _mcp_tool_definitions() -> list[dict]:
    return [
        {
            "name": "heysure.get_context",
            "description": "Read the controlled member's current prompt, bound devices and configured MCP scope.",
            "inputSchema": {"type": "object", "properties": {}, "additionalProperties": False},
        },
        {
            "name": "heysure.list_mcp_tools",
            "description": "List the member's configured HeySure MCP tools and available schemas.",
            "inputSchema": {"type": "object", "properties": {}, "additionalProperties": False},
        },
        {
            "name": "heysure.call_mcp",
            "description": "Call one HeySure server/device MCP tool through the member's existing permission checks.",
            "inputSchema": {
                "type": "object",
                "required": ["tool"],
                "properties": {
                    "tool": {"type": "string"},
                    "arguments": {"type": "object", "default": {}},
                    "run_id": {"type": "string"},
                },
                "additionalProperties": False,
            },
        },
        {
            "name": "heysure.start_run",
            "description": "Open a journaled external-controller run before starting a unit of work.",
            "inputSchema": {
                "type": "object",
                "properties": {"title": {"type": "string"}, "lease_seconds": {"type": "integer", "minimum": 30, "maximum": 1800}},
                "additionalProperties": False,
            },
        },
        {
            "name": "heysure.finish_run",
            "description": "Move an active external-controller run to an immutable terminal state.",
            "inputSchema": {
                "type": "object",
                "required": ["run_id", "status"],
                "properties": {
                    "run_id": {"type": "string"},
                    "status": {"type": "string", "enum": ["succeeded", "failed", "cancelled", "expired"]},
                    "summary": {"type": "string"},
                    "error": {"type": "string"},
                },
                "additionalProperties": False,
            },
        },
        {
            "name": "heysure.list_events",
            "description": "Read the recent control journal. Inputs are never persisted; only sanitized results are returned.",
            "inputSchema": {
                "type": "object",
                "properties": {"limit": {"type": "integer", "minimum": 1, "maximum": 500}},
                "additionalProperties": False,
            },
        },
    ]


def _configured_tool_catalog(cfg) -> list[dict]:
    try:
        from api.services.mcp.capability_view import scoped_tool_view_for_ids

        names = set(scoped_tool_view_for_ids(cfg.user_id, cfg.id).eligible_names)
    except Exception:
        names = set()
    definitions = {str(item.get("name") or ""): item for item in registry.list_tools()}
    return [definitions.get(name, {"name": name, "description": "", "inputSchema": {}}) for name in sorted(names)]


def _valid_run_id(session: Session, credential: ExternalControllerCredential, raw: Any) -> Optional[str]:
    run_id = str(raw or "").strip()
    if not run_id:
        return None
    row = session.exec(
        select(ExternalControllerRun).where(
            ExternalControllerRun.run_id == run_id,
            ExternalControllerRun.credential_id == credential.id,
        )
    ).first()
    if not row or row.status != "running":
        raise HTTPException(status_code=409, detail="run_id is not an active controller run")
    if row.lease_expires_at and row.lease_expires_at <= time.time():
        raise HTTPException(status_code=409, detail="controller run lease has expired")
    return run_id


async def _tool_context(args, service, credential, user, cfg):
    return service.context_snapshot(credential, cfg)


async def _tool_catalog(args, service, credential, user, cfg):
    return {"tools": _configured_tool_catalog(cfg)}


async def _tool_start_run(args, service, credential, user, cfg):
    row = service.start_run(
        credential, args.get("title", ""), args.get("lease_seconds", 300)
    )
    return _run_payload(row)


async def _tool_finish_run(args, service, credential, user, cfg):
    row = service.finish_run(
        credential,
        str(args.get("run_id") or ""),
        str(args.get("status") or ""),
        str(args.get("summary") or ""),
        str(args.get("error") or ""),
    )
    return _run_payload(row)


async def _tool_events(args, service, credential, user, cfg):
    return {"events": service.list_events(user.id, cfg.id, int(args.get("limit", 100)))}


async def _tool_call_mcp(args, service, credential, user, cfg):
    run_id = _valid_run_id(service.session, credential, args.get("run_id"))
    tool_name = str(args.get("tool") or "").strip()
    if not tool_name:
        raise HTTPException(status_code=400, detail="tool is required")
    try:
        local_token = create_access_token(data={
            "sub": user.account,
            "user_id": user.id,
            "auth_version": user.auth_version,
        })
        result = await call_mcp_tool(
            MCPCallRequest(tool=tool_name, arguments=args.get("arguments") or {}, ai_config_id=cfg.id),
            service.session,
            f"Bearer {local_token}",
        )
    except Exception as exc:
        detail = getattr(exc, "detail", None) or type(exc).__name__
        service.add_event(
            credential, "mcp.result", run_id=run_id, tool_name=tool_name,
            status="error", result={"error": detail},
        )
        raise
    service.add_event(
        credential, "mcp.result", run_id=run_id, tool_name=tool_name, result=result
    )
    return result


_CONTROLLER_TOOL_HANDLERS = {
    "heysure.get_context": _tool_context,
    "heysure.list_mcp_tools": _tool_catalog,
    "heysure.start_run": _tool_start_run,
    "heysure.finish_run": _tool_finish_run,
    "heysure.list_events": _tool_events,
    "heysure.call_mcp": _tool_call_mcp,
}
_CONTROLLER_TOOL_SCOPES = {
    "heysure.get_context": "context:read",
    "heysure.list_mcp_tools": "context:read",
    "heysure.start_run": "run:write",
    "heysure.finish_run": "run:write",
    "heysure.list_events": "audit:read",
    "heysure.call_mcp": "mcp:call",
}


async def _call_controller_tool(name: str, args: dict, service: ExternalControlService, credential, user, cfg) -> Any:
    handler = _CONTROLLER_TOOL_HANDLERS.get(name)
    if not handler:
        raise HTTPException(status_code=404, detail=f"Unknown controller tool: {name}")
    service.require_scope(credential, _CONTROLLER_TOOL_SCOPES[name])
    return await handler(args, service, credential, user, cfg)


def _rpc_response(rpc_id: Any, result: Any, headers: Optional[Dict] = None) -> JSONResponse:
    return JSONResponse({"jsonrpc": "2.0", "id": rpc_id, "result": result}, headers=headers or {})


async def _rpc_initialize(payload, context):
    requested = str((payload.get("params") or {}).get("protocolVersion") or PROTOCOL_VERSION)
    result = {
        "protocolVersion": requested,
        "capabilities": {"tools": {"listChanged": False}},
        "serverInfo": {"name": "HeySure External Controller", "version": "1.0.0"},
        "instructions": "Call heysure.get_context first. Wrap work in start_run/finish_run and execute all actions through heysure.call_mcp.",
    }
    return _rpc_response(payload.get("id"), result, context[4])


async def _rpc_ping(payload, context):
    return _rpc_response(payload.get("id"), {}, context[4])


async def _rpc_tools_list(payload, context):
    return _rpc_response(payload.get("id"), {"tools": _mcp_tool_definitions()}, context[4])


async def _rpc_tools_call(payload, context):
    service, credential, user, cfg, headers = context
    params = payload.get("params") or {}
    name = str(params.get("name") or "")
    args = params.get("arguments") if isinstance(params.get("arguments"), dict) else {}
    try:
        result = await _call_controller_tool(name, args, service, credential, user, cfg)
        text = json.dumps(result, ensure_ascii=False, default=str)
        structured = result if isinstance(result, dict) else {"result": result}
        return _rpc_response(
            payload.get("id"),
            {"content": [{"type": "text", "text": text}], "structuredContent": structured},
            headers,
        )
    except HTTPException as exc:
        message = str(exc.detail)
    except Exception as exc:
        message = f"Controller tool failed: {type(exc).__name__}"
    return _rpc_response(
        payload.get("id"),
        {"content": [{"type": "text", "text": message}], "isError": True},
        headers,
    )


_RPC_HANDLERS = {
    "initialize": _rpc_initialize,
    "ping": _rpc_ping,
    "tools/list": _rpc_tools_list,
    "tools/call": _rpc_tools_call,
}


async def _read_rpc_payload(request: Request) -> dict:
    try:
        payload = await request.json()
    except Exception as exc:
        raise HTTPException(status_code=400, detail="Invalid JSON-RPC payload") from exc
    if not isinstance(payload, dict) or payload.get("jsonrpc") != "2.0":
        raise HTTPException(status_code=400, detail="JSON-RPC 2.0 object required")
    return payload


def _method_not_found(payload: dict, headers: dict) -> JSONResponse:
    return JSONResponse(
        {"jsonrpc": "2.0", "id": payload.get("id"), "error": {"code": -32601, "message": "Method not found"}},
        status_code=200,
        headers=headers,
    )


@router.post("/mcp/external")
async def external_mcp(
    request: Request,
    session: Session = Depends(get_session),
    authorization: str = Header(None),
):
    service = ExternalControlService(session)
    credential, user, cfg = service.authenticate(authorization)
    payload = await _read_rpc_payload(request)
    method = str(payload.get("method") or "")
    if method.startswith("notifications/"):
        return Response(status_code=202)
    session_headers = {"Mcp-Session-Id": f"hsc-{credential.id}"}
    handler = _RPC_HANDLERS.get(method)
    if not handler:
        return _method_not_found(payload, session_headers)
    context = (service, credential, user, cfg, session_headers)
    return await handler(payload, context)


@router.get("/mcp/external")
def external_mcp_get() -> Response:
    return Response(status_code=405, headers={"Allow": "POST"})
