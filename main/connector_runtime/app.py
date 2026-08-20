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

from connector_runtime.bots import backfill_legacy_connection_directories, iter_bots
from api.database import create_db_and_tables
from api.models import AssistantAIConfig
from api.sio import sio
from connector_runtime.socket_handlers.assembly import register_agent_socket_events
from api.runtime.internal_http import require_internal_token
from api.core.settings import settings


logger = logging.getLogger(__name__)


class DeviceDispatchRequest(BaseModel):
    user_id: int
    ai_config_id: Optional[int] = None
    tool: str
    arguments: Dict[str, Any] = Field(default_factory=dict)
    timeout_seconds: int = 120


class DeviceDispatchExpireRequest(BaseModel):
    reason: str = "result wait timed out"


class DeviceDispatchCancelRequest(BaseModel):
    reason: str = "cancelled by caller"


class FeishuSendRequest(BaseModel):
    user_id: int
    ai_config_id: Optional[int] = None
    text: str
    receive_id: Optional[str] = None
    receive_id_type: Optional[str] = None


def _start_conversation_bridge(stop_event: asyncio.Event) -> asyncio.Task:
    from connector_runtime.maintenance_conversation_bridge import run_conversation_bridge

    return asyncio.create_task(
        run_conversation_bridge(stop_event), name="codex-conversation-bridge"
    )


class BotLoginRequest(BaseModel):
    user_id: int
    connection_ref: str = ""


class BotVerifyCodeRequest(BaseModel):
    value: str
    connection_ref: str = ""


class DeviceUpdateBroadcastRequest(BaseModel):
    product_id: str
    target_id: str
    latest_version: str
    mandatory: bool = False
    release_notes: str = ""


@asynccontextmanager
async def _lifespan(app: FastAPI):
    from api.runtime.health import state_for

    health = state_for("connector")
    health.mark_not_ready("schema check")
    create_db_and_tables()
    # Existing deployments stored one channel config directly on the AI row.
    # Backfill only missing directory rows; never overwrite instance edits.
    backfill_legacy_connection_directories()
    # This process owns endpoint-agent sockets in split deployments. Its
    # in-memory socket registry is empty after a restart, so clear stale shared
    # presence before surviving agents reconnect and register themselves.
    from api.devices.socket_owner import should_reset_endpoint_presence
    if should_reset_endpoint_presence(settings.service_role, settings.connector_runtime_url):
        try:
            from api.devices.presence import mark_all_offline
            mark_all_offline()
        except Exception:
            logger.exception("failed to reset endpoint agent presence on startup")
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
    from connector_runtime.dispatch.user_push_scheduler import run_user_push_scheduler

    push_task = asyncio.create_task(
        run_user_push_scheduler(stop_event), name="user-push-scheduler"
    )
    conversation_bridge_task = _start_conversation_bridge(stop_event)
    workflow_task = None
    if settings.workflow_scheduler_enabled:
        from connector_runtime.dispatch.workflow_scheduler import run_workflow_scheduler

        workflow_task = asyncio.create_task(
            run_workflow_scheduler(stop_event), name="workflow-scheduler"
        )
    bot_channels = ",".join(bot.channel for bot in iter_bots()) or "no bots"
    logger.info(f"ready (Socket.IO + /internal/* + bot keepalive: {bot_channels})")
    health.mark_ready()
    try:
        yield
    finally:
        health.begin_draining()
        stop_event.set()
        background_tasks = [*keepalive_tasks, sweep_task, push_task, conversation_bridge_task]
        if workflow_task is not None:
            background_tasks.append(workflow_task)
        for task in background_tasks:
            task.cancel()
            with suppress(asyncio.CancelledError):
                await task


def _new_fastapi_app() -> FastAPI:
    fastapi_app = FastAPI(title="HeySure Connector Runtime", lifespan=_lifespan)
    from api.runtime.log_context import install_http_request_context
    install_http_request_context(fastapi_app)
    return fastapi_app


def _register_dispatch_cancel_route(router: APIRouter) -> None:
    @router.post("/agent/dispatch/cancel/{task_id}")
    async def device_dispatch_cancel(
        task_id: str, req: Optional[DeviceDispatchCancelRequest] = None
    ) -> Dict[str, Any]:
        from connector_runtime.dispatch.device_dispatch import cancel_dispatch

        cancelled = await cancel_dispatch(
            task_id,
            reason=(req.reason if req else "cancelled by caller"),
        )
        return {"ok": True, "cancelled": cancelled}


def _register_control_routes(router: APIRouter) -> None:
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

    _register_bot_login_routes(router)


def _qr_bot(channel: str, method_name: str):
    from connector_runtime.bots import get as get_bot

    bot = get_bot(channel)
    if bot is None or not hasattr(bot, method_name):
        raise HTTPException(status_code=404, detail="bot channel does not support QR login")
    return bot


def _register_bot_login_routes(router: APIRouter) -> None:
    @router.post("/bot/{channel}/login/{config_id}")
    def bot_login(channel: str, config_id: int, req: BotLoginRequest) -> Dict[str, Any]:
        return _qr_bot(channel, "start_login").start_login(config_id, req.user_id, req.connection_ref)

    @router.get("/bot/{channel}/login/{config_id}")
    def bot_login_status(channel: str, config_id: int, connection_ref: str = "") -> Dict[str, Any]:
        return _qr_bot(channel, "login_status").login_status(config_id, connection_ref)

    @router.post("/bot/{channel}/login/{config_id}/verify-code")
    def bot_login_verify_code(channel: str, config_id: int, req: BotVerifyCodeRequest) -> Dict[str, Any]:
        try:
            return _qr_bot(channel, "submit_login_verify_code").submit_login_verify_code(config_id, req.value, req.connection_ref)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    @router.post("/bot/{channel}/logout/{config_id}")
    def bot_logout(channel: str, config_id: int, connection_ref: str = "") -> Dict[str, Any]:
        return _qr_bot(channel, "logout").logout(config_id, connection_ref)


def _bot_runtime_statuses() -> Dict[str, Any]:
    from sqlmodel import Session, select
    from api.database import engine
    from api.models import BotConnection

    with Session(engine) as session:
        configs = session.exec(select(AssistantAIConfig)).all()
        connections = session.exec(select(BotConnection).where(
            BotConnection.enabled.is_(True), BotConnection.state != "deleted"
        )).all()
    bots = list(iter_bots())
    per_channel: Dict[str, Dict[str, Dict[str, str]]] = {bot.channel: {} for bot in bots}
    for cfg in configs:
        if cfg.id:
            for bot in bots:
                per_channel[bot.channel][str(cfg.id)] = bot.get_long_connection_state(int(cfg.id))
    payload: Dict[str, Any] = {
        "ok": True,
        **{f"{channel}_statuses": states for channel, states in per_channel.items()},
    }
    payload["connection_statuses"] = {
        row.connection_ref: next(
            bot.get_long_connection_state(int(row.ai_config_id), row.connection_ref)
            for bot in bots if bot.channel == row.channel
        )
        for row in connections
    }
    return payload


def create_app() -> FastAPI:
    fastapi_app = _new_fastapi_app()

    router = APIRouter(prefix="/internal", dependencies=[Depends(require_internal_token)])
    _register_dispatch_cancel_route(router)
    _register_control_routes(router)

    @router.post("/maintenance/command")
    async def maintenance_command(req: Dict[str, Any]) -> Dict[str, Any]:
        from connector_runtime.maintenance import MaintenanceCommandRequest, send_command

        return await send_command(MaintenanceCommandRequest.model_validate(req))

    from api.runtime.health import build_health_router
    from connector_runtime.health_detail import connector_health_detail

    router.include_router(build_health_router("connector", detail_provider=connector_health_detail))

    @router.post("/device-updates/broadcast")
    async def broadcast_device_update(req: DeviceUpdateBroadcastRequest) -> Dict[str, Any]:
        await sio.emit("device:update-available", req.model_dump())
        return {"ok": True}

    @router.get("/bot/statuses")
    def bot_statuses() -> Dict[str, Any]:
        """Return ``{<channel>_statuses: {config_id: state}}`` for every bot.

        api-gateway's bot status route consumes this; the shape stays
        ``"<channel>_statuses"`` so existing clients keep working but the
        set of keys grows automatically when new bots register.
        """
        return _bot_runtime_statuses()

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

    @router.post("/agent/dispatch/expire/{task_id}")
    async def device_dispatch_expire(
        task_id: str, req: Optional[DeviceDispatchExpireRequest] = None
    ) -> Dict[str, Any]:
        """Finalize a dispatch whose remote caller stopped waiting.

        AI Runtime polls Connector Runtime directly in split deployments, so
        the expire endpoint must live beside the dispatch/result endpoints.
        The Gateway mirror alone cannot unblock Connector's device queue.
        """
        from connector_runtime.dispatch.device_dispatch import expire_dispatch

        expired = await expire_dispatch(
            task_id,
            reason=(req.reason if req else "result wait timed out"),
        )
        return {"ok": True, "expired": expired}

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
