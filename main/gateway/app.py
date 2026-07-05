"""``api-gateway`` FastAPI + Socket.IO app served on port 3000.

Mounts every router under ``api/routers/`` at ``/api`` and wraps the
Socket.IO server from ``api.sio``. Shared library code lives in ``api``;
this module only wires it together for the public-facing process.
"""

import socketio
import logging
import os
import sys
import importlib
import glob
import asyncio
import threading
import time
from contextlib import asynccontextmanager, suppress
from pathlib import Path
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles

from connector_runtime.bots import iter_bots
from api.core.logging_config import configure_logging
from api.core.settings import settings
from api.sio import sio
from api.socket_events import register_agent_socket_events, register_user_socket_events
from api.database import create_db_and_tables

from api.runtime import heartbeat as heartbeat_module
from ai_runtime.inference.ai_service import align_token_snapshots_with_history, migrate_legacy_switch_files_to_db
from api.chat_runtime.chat_scheduler import process_task_scheduler
from api.services.storage.temp_image_store import cleanup_expired_temp_images
from api.services import repo_update


# Ensure logging is configured when the app is loaded by uvicorn (which
# may not go through gateway.main).
configure_logging()
logger = logging.getLogger(__name__)

# Declare how to relaunch ourselves on an admin-triggered restart. Routing
# through ``gateway.main`` means it works whether we were started via the
# uvicorn CLI or ``python -m gateway.main`` (both end up serving port 3000).
from api.runtime.process_control import register_restart_command  # noqa: E402

register_restart_command([sys.executable, "-m", "gateway.main"])


@asynccontextmanager
async def lifespan(app: FastAPI):
    create_db_and_tables()
    # Endpoint agents register their sockets here; a fresh boot starts with an
    # empty in-memory registry, so reset the shared presence snapshot — agents
    # flip their own rows back online as they reconnect.
    try:
        from api.devices.presence import mark_all_offline
        mark_all_offline()
    except Exception:
        logger.exception("failed to reset endpoint agent presence on startup")
    try:
        result = migrate_legacy_switch_files_to_db()
        if result.get("imported") or result.get("removed"):
            logger.info(
                f"migrate_legacy_switch_files_to_db imported={result.get('imported', 0)} "
                f"removed={result.get('removed', 0)}"
            )
    except Exception:
        logger.exception("migrate_legacy_switch_files_to_db failed")
    def _run_alignment():
        try:
            t0 = time.time()
            result = align_token_snapshots_with_history()
            dt = time.time() - t0
            if result.get("changed_rows") or result.get("deleted_rows") or dt > 1.0:
                logger.info(
                    f"align_token_snapshots_with_history changed={result.get('changed_rows', 0)} "
                    f"deleted={result.get('deleted_rows', 0)} took={dt:.2f}s"
                )
        except Exception:
            logger.exception("align_token_snapshots_with_history failed")

    # Run heavy reconciliation in background so lifespan yields quickly and
    # the gateway becomes available faster on restart. Previously this could
    # make the whole server "卡" for a long time.
    threading.Thread(target=_run_alignment, daemon=True, name="token-align").start()
    stop_event = asyncio.Event()

    watchdog_counter = {"ticks": 0}
    # In split deployments the dedicated connector-runtime owns every bot's
    # long-connection client so api-gateway restarts don't drop upstream.
    _bots_owned_by_gateway = not settings.connector_runtime_url

    async def periodic_scan():
        while not stop_event.is_set():
            if _bots_owned_by_gateway:
                for bot in iter_bots():
                    try:
                        bot.start_long_connections()
                    except Exception:
                        logger.exception(f"start {bot.channel} long_connections failed")
            try:
                process_task_scheduler()
            except Exception:
                logger.exception("process_task_scheduler failed")
            # Reap stale ChatRun heartbeats every ~30s to avoid scanning
            # the table on every 3s tick.
            watchdog_counter["ticks"] += 1
            if watchdog_counter["ticks"] % 10 == 0:
                try:
                    reaped = heartbeat_module.reap_stale_runs()
                    if reaped:
                        logger.warning(f"watchdog reaped stale runs: {reaped}")
                except Exception:
                    logger.exception("watchdog reap failed")
                try:
                    removed = cleanup_expired_temp_images()
                    if removed:
                        logger.info(f"removed expired temporary images: {removed}")
                except Exception:
                    logger.exception("temporary image cleanup failed")
                # Repo auto-update: cheap eligibility check (~every 30s); the
                # actual fetch/pull/restart runs on a background thread when due.
                try:
                    repo_update.maybe_auto_check()
                except Exception:
                    logger.exception("repo-update auto check failed")
            await asyncio.sleep(3)

    task = asyncio.create_task(periodic_scan())
    yield
    stop_event.set()
    task.cancel()
    with suppress(asyncio.CancelledError):
        await task

# FastAPI App
app = FastAPI(lifespan=lifespan)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Serve the preset avatar images so clients (browser extension / desktop agent)
# can fetch them from the backend instead of bundling their own copies. The
# stored ``user.avatar`` value resolves to ``/avatars/avatarsN.png`` here.
_avatars_dir = Path(__file__).resolve().parent.parent.parent / "static" / "avatars"
if _avatars_dir.is_dir():
    app.mount("/avatars", StaticFiles(directory=str(_avatars_dir)), name="avatars")
else:
    logger.warning("avatars static dir not found: %s", _avatars_dir)

# Serve AI member preset avatars (used for digital life cards and workshop bindings).
_ai_avatars_dir = Path(__file__).resolve().parent.parent.parent / "static" / "ai_avatars"
if _ai_avatars_dir.is_dir():
    app.mount("/ai_avatars", StaticFiles(directory=str(_ai_avatars_dir)), name="ai_avatars")
else:
    logger.warning("ai_avatars static dir not found: %s", _ai_avatars_dir)

# Preset device icons. A device may pick one at register time (icon: "3" →
# /device_png/3.webp, see api.devices.presence.normalize_device_icon); devices
# that pick none keep the web's built-in per-type rendering.
_device_png_dir = Path(__file__).resolve().parent.parent.parent / "static" / "device_png"
if _device_png_dir.is_dir():
    app.mount("/device_png", StaticFiles(directory=str(_device_png_dir)), name="device_png")
else:
    logger.warning("device_png static dir not found: %s", _device_png_dir)

# 自动注册路由：扫描 gateway/routers/ 目录下所有 HTTP 路由模块。
# 网关进程现在拥有所有真路由；推理 helper（chat_prompt_utils/chat_runtime_helpers/
# chat_scheduler/chat_stream）已迁到 api/chat_runtime/，作为跨 runtime 共享代码。
import gateway.routers as _routers_pkg
routers_dir = os.path.dirname(_routers_pkg.__file__)
router_files = glob.glob(os.path.join(routers_dir, "*.py"))

for router_file in router_files:
    module_name = os.path.basename(router_file)[:-3]
    if module_name == "__init__":
        continue

    try:
        module = importlib.import_module(f"gateway.routers.{module_name}")
        # 仅注册显式入口路由模块，避免拆分后的子模块重复挂载同一 router
        if hasattr(module, "router") and getattr(module, "IS_ROUTER_ENTRY", True):
            # 默认统一挂载到 /api 前缀下；模块可定义 PREFIX 覆盖。
            prefix = getattr(module, "PREFIX", "/api")
            app.include_router(module.router, prefix=prefix)
            logger.info(f"loaded router: {module_name} -> {prefix}")
    except Exception:
        logger.exception(f"failed to load router {module_name}")

# Bot routers live next to each bot's adapter — mount them here so adding a
# new bot stays a single ``bots/<name>/`` directory drop instead of also
# editing api/routers/.
for _bot in iter_bots():
    try:
        bot_router_module = importlib.import_module(f"connector_runtime.bots.{_bot.channel}.router")
    except ModuleNotFoundError:
        continue
    bot_router = getattr(bot_router_module, "router", None)
    if bot_router is None:
        continue
    bot_prefix = getattr(bot_router_module, "PREFIX", f"/api/{_bot.channel}")
    app.include_router(bot_router, prefix=bot_prefix)
    logger.info(f"loaded bot router: {_bot.channel} -> {bot_prefix}")

# Register Socket Events. Desktop / browser agents connect to the api-gateway
# (the single public URL they also use for REST auth), so the gateway owns the
# agent-side handlers + the live ``agents`` registry in every deployment. Task
# dispatch is therefore served from here too (see gateway.routers.
# device_dispatch_internal); ai-runtime routes endpoint-tool dispatches to this
# process via HEYSURE_API_GATEWAY_URL.
register_user_socket_events()
register_agent_socket_events()

# Socket.IO App Wrapper
sio_app = socketio.ASGIApp(sio, other_asgi_app=app)

@app.get("/")
async def root():
    return {"message": "HeySure Server is running"}
