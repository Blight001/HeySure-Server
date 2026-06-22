"""``mcp-runtime`` FastAPI app — wraps the MCP registry over HTTP.

Endpoints (all under ``/internal``, gated by ``INTERNAL_TOKEN`` Bearer):
- ``GET  /internal/health``
- ``GET  /internal/mcp/tools``  — raw registry catalog + version
- ``POST /internal/mcp/call``   — invoke a tool by name (body carries user_id, ai_config_id, arguments)
- ``POST /internal/mcp/reload`` — hot-reload tool modules

Auth/permission filtering belongs to the api-gateway side. This service
trusts its callers — they must already be authenticated by their own
ingress layer.
"""

from __future__ import annotations

import logging
from contextlib import asynccontextmanager
from typing import Any, Dict, Optional

from fastapi import APIRouter, Depends, FastAPI, HTTPException
from pydantic import BaseModel, Field

from api.database import create_db_and_tables
from mcp_runtime.mcp import registry
from mcp_runtime.mcp.loader import reload_registry
from api.runtime.internal_http import require_internal_token


logger = logging.getLogger(__name__)


class CallRequest(BaseModel):
    tool: str = Field(..., description="MCP tool name (e.g. workspace.read_file)")
    user_id: int = Field(..., description="Acting user id; tools scope file/DB access to this user")
    ai_config_id: Optional[int] = None
    arguments: Optional[Dict[str, Any]] = Field(default_factory=dict)
    # Snapshot of the calling run's session context (session_id / channel /
    # identity / current_user_message_id). In split deployments the worker's
    # contextvar does not cross the process boundary, so the caller serializes
    # it here and we re-establish it for the duration of the tool call.
    session_context: Optional[Dict[str, Any]] = None


def _tool_catalog() -> Dict[str, Any]:
    return {
        "version": registry.version,
        "tools": registry.list_tools(),
    }


@asynccontextmanager
async def _lifespan(app: FastAPI):
    create_db_and_tables()
    # Built-in tools are registered when the registry module is imported.
    from mcp_runtime.mcp import registry as _mcp_registry
    logger.info(f"ready: {len(_mcp_registry._tools)} tools (version {_mcp_registry.version})")
    yield


def create_app() -> FastAPI:
    app = FastAPI(title="HeySure MCP Runtime", lifespan=_lifespan)
    router = APIRouter(prefix="/internal", dependencies=[Depends(require_internal_token)])

    @router.get("/health")
    def health() -> Dict[str, Any]:
        return {"ok": True, "tools": len(registry._tools), "version": registry.version}

    @router.get("/logs")
    def logs(limit: int = 200, level: Optional[str] = None) -> Dict[str, Any]:
        from api.core.logging_config import get_recent_logs

        return {"ok": True, "lines": get_recent_logs(limit=limit, level=level)}

    @router.post("/restart")
    def restart() -> Dict[str, Any]:
        from api.runtime.process_control import request_restart

        cmd = request_restart()
        logger.warning("restart requested via /internal/restart")
        return {"ok": True, "restarting": True, "command": cmd}

    @router.get("/mcp/tools")
    def list_tools() -> Dict[str, Any]:
        return _tool_catalog()

    @router.post("/mcp/call")
    async def call_tool(req: CallRequest) -> Dict[str, Any]:
        if not registry.has(req.tool):
            raise HTTPException(status_code=404, detail=f"Unknown tool: {req.tool}")
        # Re-establish the caller's run session context so tools that rely on
        # get_run_session_context() (conversation.*, communication.*) work in
        # the split (remote HTTP) deployment, then restore the previous value.
        from connector_runtime.dispatch.device_dispatch import set_run_session_context

        token = set_run_session_context(req.session_context or None)
        try:
            return await registry.call(
                req.tool,
                req.user_id,
                req.arguments or {},
                req.ai_config_id,
            )
        finally:
            try:
                from connector_runtime.dispatch.device_dispatch import _RUN_SESSION_CONTEXT

                _RUN_SESSION_CONTEXT.reset(token)
            except Exception:
                set_run_session_context(None)

    @router.post("/mcp/reload")
    def reload_tools() -> Dict[str, Any]:
        result = reload_registry()
        if not result.get("ok"):
            # 503 so callers know the reload itself failed but the live
            # registry remains intact and usable.
            raise HTTPException(status_code=503, detail=result)
        return result

    app.include_router(router)
    return app
