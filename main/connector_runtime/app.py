"""``connector-runtime`` FastAPI + Socket.IO app.

Hosts:
- Socket.IO ``/agent`` namespace (and the default ``/`` namespace as a
  compatibility shim) where desktop / browser agents register and stream
  task results.
- HTTP ``/internal/agent/dispatch``: synchronous wrapper around
  :func:`connector_runtime.dispatch.device_dispatch.dispatch_endpoint_tool_and_wait` so
  ai-runtime can fire a tool dispatch over HTTP and wait for the agent's
  reply within the same process that holds the Socket.IO session.
- HTTP ``/internal/feishu/send``: outbound Feishu helper for ai-runtime.
- HTTP ``/internal/health``.

Both the Socket.IO server and the HTTP routes share the same ASGI app —
they bind to a single external port (default 3002). ``/internal/*`` is
gated by ``INTERNAL_TOKEN``; the Socket.IO routes use the same per-agent
JWT auth as the monolith.
"""

from __future__ import annotations

import asyncio
import logging
from contextlib import asynccontextmanager, suppress
from typing import Any, Dict, Optional

import socketio
from fastapi import APIRouter, Depends, FastAPI, HTTPException
from pydantic import BaseModel, Field

from connector_runtime.bots import iter_bots
from api.database import create_db_and_tables
from api.models import AssistantAIConfig
from api.sio import sio
from api.socket_events import register_agent_socket_events
from api.runtime.internal_http import require_internal_token


logger = logging.getLogger(__name__)


class DeviceDispatchRequest(BaseModel):
    user_id: int
    ai_config_id: Optional[int] = None
    tool: str
    arguments: Dict[str, Any] = Field(default_factory=dict)
    timeout_seconds: int = 120


class FeishuSendRequest(BaseModel):
    user_id: int
    ai_config_id: Optional[int] = None
    text: str
    receive_id: Optional[str] = None
    receive_id_type: Optional[str] = None


@asynccontextmanager
async def _lifespan(app: FastAPI):
    create_db_and_tables()
    # Register Socket.IO handlers on the local server. Only agent-side
    # events live here; user-side (ui:join) stays on api-gateway.
    register_agent_socket_events()

    # Reap any dispatch rows whose original Future died with a previous
    # connector-runtime process. The poller would otherwise wait forever.
    from connector_runtime.dispatch.device_dispatch import expire_orphan_dispatches
    try:
        expired = expire_orphan_dispatches()
        if expired:
            logger.info(f"expired {expired} orphan dispatch rows")
    except Exception:
        logger.exception("orphan sweep failed")

    # Maintain every registered bot's long-connection clients from this
    # process. Owning the upstream here means api-gateway restarts no
    # longer drop the inbound messages each bot is responsible for.
    stop_event = asyncio.Event()

    def _make_bot_keepalive(bot):
        async def _keepalive() -> None:
            while not stop_event.is_set():
                try:
                    bot.start_long_connections()
                except Exception:
                    logger.exception(f"{bot.channel} keepalive failed")
                try:
                    await asyncio.wait_for(stop_event.wait(), timeout=3.0)
                except asyncio.TimeoutError:
                    continue
        return _keepalive

    async def _orphan_sweeper() -> None:
        # Periodic sweep — startup pass alone isn't enough for a process
        # that runs for days without a restart.
        while not stop_event.is_set():
            try:
                await asyncio.wait_for(stop_event.wait(), timeout=60.0)
                return  # stop_event set
            except asyncio.TimeoutError:
                pass
            try:
                expired_now = expire_orphan_dispatches()
                if expired_now:
                    logger.info(f"expired {expired_now} orphan dispatch rows")
            except Exception:
                logger.exception("periodic orphan sweep failed")

    keepalive_tasks = [
        asyncio.create_task(_make_bot_keepalive(bot)(), name=f"keepalive-{bot.channel}")
        for bot in iter_bots()
    ]
    sweep_task = asyncio.create_task(_orphan_sweeper())
    bot_channels = ",".join(bot.channel for bot in iter_bots()) or "no bots"
    logger.info(f"ready (Socket.IO + /internal/* + bot keepalive: {bot_channels})")
    try:
        yield
    finally:
        stop_event.set()
        for task in (*keepalive_tasks, sweep_task):
            task.cancel()
            with suppress(asyncio.CancelledError):
                await task


def create_app() -> FastAPI:
    fastapi_app = FastAPI(title="HeySure Connector Runtime", lifespan=_lifespan)

    router = APIRouter(prefix="/internal", dependencies=[Depends(require_internal_token)])

    @router.get("/health")
    def health() -> Dict[str, Any]:
        from api.sio import agents
        return {"ok": True, "agents": len(agents)}

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

    @router.get("/bot/statuses")
    def bot_statuses() -> Dict[str, Any]:
        """Return ``{<channel>_statuses: {config_id: state}}`` for every bot.

        api-gateway's bot status route consumes this; the shape stays
        ``"<channel>_statuses"`` so existing clients keep working but the
        set of keys grows automatically when new bots register.
        """
        from sqlmodel import Session, select
        from api.database import engine

        per_channel: Dict[str, Dict[str, Dict[str, str]]] = {
            bot.channel: {} for bot in iter_bots()
        }
        with Session(engine) as session:
            configs = session.exec(select(AssistantAIConfig)).all()
        for cfg in configs:
            config_id = int(cfg.id or 0)
            if not config_id:
                continue
            for bot in iter_bots():
                per_channel[bot.channel][str(config_id)] = bot.get_long_connection_state(config_id)

        payload: Dict[str, Any] = {"ok": True}
        for channel, statuses in per_channel.items():
            payload[f"{channel}_statuses"] = statuses
        return payload

    @router.post("/agent/dispatch")
    async def device_dispatch(req: DeviceDispatchRequest) -> Dict[str, Any]:
        # Non-blocking: emit task:dispatch to the agent + persist a pending
        # row. The caller polls /agent/dispatch/result/{task_id} for the
        # outcome so connector-runtime restarts don't strand the request.
        from connector_runtime.dispatch.device_dispatch import dispatch_endpoint_tool
        try:
            task_id = await dispatch_endpoint_tool(
                user_id=req.user_id,
                ai_config_id=req.ai_config_id,
                tool=req.tool,
                args=req.arguments,
            )
        except Exception as exc:
            raise HTTPException(status_code=500, detail=f"dispatch failed: {exc}")
        if not task_id:
            raise HTTPException(status_code=503, detail="no agent connected for this tool")
        from sqlmodel import Session, select
        from api.database import engine
        from api.models import AgentDispatchTask
        with Session(engine) as session:
            row = session.exec(
                select(AgentDispatchTask).where(AgentDispatchTask.task_id == task_id)
            ).first()
        return {"ok": True, "task_id": task_id, "status": row.status if row else "pending"}

    @router.get("/agent/dispatch/result/{task_id}")
    def device_dispatch_result(task_id: str) -> Dict[str, Any]:
        # DB-backed lookup so connector-runtime restarts don't lose state.
        from sqlmodel import Session, select
        from api.database import engine
        from api.models import AgentDispatchTask
        with Session(engine) as session:
            row = session.exec(
                select(AgentDispatchTask).where(AgentDispatchTask.task_id == task_id)
            ).first()
        if not row:
            raise HTTPException(status_code=404, detail="task not found")
        payload: Dict[str, Any] = {
            "task_id": row.task_id,
            "status": row.status,
            "success": row.success,
            "summary": row.summary,
            "error": row.error,
            "result": None,
            "device_id": row.device_id,
            "tool": row.tool,
        }
        if row.result_json:
            import json as _json
            try:
                payload["result"] = _json.loads(row.result_json)
            except Exception:
                payload["result"] = row.result_json
        return payload

    @router.post("/feishu/send")
    def feishu_send(req: FeishuSendRequest) -> Dict[str, Any]:
        # Lark-oapi is loaded lazily inside the adapter so processes that
        # never send outbound Feishu traffic don't pull it in at import time.
        from connector_runtime.bots.messaging import dispatcher

        try:
            delivery = dispatcher.send_text(
                user_id=req.user_id,
                ai_config_id=req.ai_config_id,
                channel="feishu",
                text=req.text,
                raw_target={
                    "receive_id": req.receive_id or "",
                    "receive_id_type": req.receive_id_type or "",
                },
            )
        except Exception as exc:
            raise HTTPException(status_code=500, detail=f"feishu send failed: {exc}")
        return {"ok": True, "result": delivery.detail}

    fastapi_app.include_router(router)

    # Combine FastAPI + Socket.IO on one ASGI app so they share a single
    # external port. ``sio`` is the real Socket.IO server because this
    # process runs with HEYSURE_SERVICE_ROLE=connector (see api.sio).
    return socketio.ASGIApp(sio, other_asgi_app=fastapi_app)
