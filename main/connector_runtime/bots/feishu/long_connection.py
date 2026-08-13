import asyncio
import json
import logging
import threading
from concurrent.futures import Future
from typing import Any, Dict, Optional, Tuple

from sqlmodel import Session, select

from api.database import engine
from api.models import BotConnection
from api.services.bot_directory import connection_config
from ._config import FEISHU_DEFAULTS
from .router import handle_feishu_event_payload


logger = logging.getLogger(__name__)

_LOCK = threading.Lock()
_LOOP_LOCK = threading.Lock()
_CLIENTS: Dict[str, Any] = {}
_PING_TASKS: Dict[str, asyncio.Task] = {}
_SIGNATURES: Dict[str, Tuple[str, str]] = {}
_STARTING_CONFIG_IDS: set[str] = set()
_LAST_ERRORS: Dict[str, str] = {}
_CONFIG_IDS: Dict[str, int] = {}
_LOOP: Optional[asyncio.AbstractEventLoop] = None
_LOOP_THREAD: Optional[threading.Thread] = None


def _is_normal_lark_close(exc: BaseException) -> bool:
    name = exc.__class__.__name__
    if name in {"ConnectionClosedOK", "ConnectionClosed"} and "1000" in str(exc):
        return True
    return "Close(code=1000" in str(exc)


def _ignore_normal_lark_loop_exception(loop: asyncio.AbstractEventLoop, context: Dict[str, Any]) -> None:
    exc = context.get("exception")
    if isinstance(exc, BaseException) and _is_normal_lark_close(exc):
        return
    loop.default_exception_handler(context)


def _ensure_lark_loop():
    import lark_oapi.ws.client as lark_ws_client

    global _LOOP, _LOOP_THREAD
    loop = lark_ws_client.loop
    loop.set_exception_handler(_ignore_normal_lark_loop_exception)
    with _LOOP_LOCK:
        _LOOP = loop
        if loop.is_running():
            return loop
        if _LOOP_THREAD and _LOOP_THREAD.is_alive():
            return loop

        def run_loop() -> None:
            asyncio.set_event_loop(loop)
            try:
                loop.run_forever()
            except RuntimeError as exc:
                if "already running" not in str(exc):
                    raise

        _LOOP_THREAD = threading.Thread(
            target=run_loop,
            name="feishu-ws-loop",
            daemon=True,
        )
        _LOOP_THREAD.start()
    return loop


def _build_event_handler(lark, config_id: int, connection_ref: str):
    def do_p2_im_message_receive_v1(data) -> None:
        try:
            raw = lark.JSON.marshal(data)
            payload = raw if isinstance(raw, dict) else json.loads(raw)
            handle_feishu_event_payload(config_id, payload, verify_token=False, connection_ref=connection_ref)
        except Exception:
            logger.exception(f"handle event failed config_id={config_id}")

    return (
        lark.EventDispatcherHandler.builder("", "")
        .register_p2_im_message_receive_v1(do_p2_im_message_receive_v1)
        .build()
    )


def _build_client(lark, config_id: int, connection_ref: str, app_id: str, app_secret: str):
    event_handler = _build_event_handler(lark, config_id, connection_ref)
    try:
        return lark.ws.Client(
            app_id,
            app_secret,
            event_handler=event_handler,
            log_level=lark.LogLevel.DEBUG,
            auto_reconnect=True,
        )
    except TypeError:
        return lark.ws.Client(
            app_id,
            app_secret,
            event_handler=event_handler,
            log_level=lark.LogLevel.DEBUG,
        )


async def _connect_client(connection_ref: str, client: Any) -> None:
    try:
        logger.info("starting connection_ref=%s", connection_ref)
        await client._connect()
        ping_task = asyncio.create_task(client._ping_loop())
        should_disconnect = False
        with _LOCK:
            if _CLIENTS.get(connection_ref) is client:
                _PING_TASKS[connection_ref] = ping_task
                _STARTING_CONFIG_IDS.discard(connection_ref)
                _LAST_ERRORS.pop(connection_ref, None)
            else:
                should_disconnect = True
        if should_disconnect:
            ping_task.cancel()
            client._auto_reconnect = False
            await client._disconnect()
    except Exception as exc:
        logger.warning("stopped connection_ref=%s error_type=%s", connection_ref, type(exc).__name__)
        with _LOCK:
            if _CLIENTS.get(connection_ref) is client:
                _CLIENTS.pop(connection_ref, None)
                _PING_TASKS.pop(connection_ref, None)
                _SIGNATURES.pop(connection_ref, None)
                _STARTING_CONFIG_IDS.discard(connection_ref)
                _LAST_ERRORS[connection_ref] = str(exc)


async def _disconnect_client(connection_ref: str, client: Any, ping_task: Optional[asyncio.Task]) -> None:
    try:
        client._auto_reconnect = False
        if ping_task:
            ping_task.cancel()
        await client._disconnect()
        logger.info("disconnected connection_ref=%s", connection_ref)
    except Exception as exc:
        logger.exception(f"disconnect failed config_id={config_id}")
        with _LOCK:
            _LAST_ERRORS[connection_ref] = str(exc)


def _schedule_disconnect_locked(connection_ref: str) -> Optional[Future]:
    client = _CLIENTS.pop(connection_ref, None)
    ping_task = _PING_TASKS.pop(connection_ref, None)
    _SIGNATURES.pop(connection_ref, None)
    _STARTING_CONFIG_IDS.discard(connection_ref)
    _LAST_ERRORS.pop(connection_ref, None)
    _CONFIG_IDS.pop(connection_ref, None)
    if client is None or _LOOP is None:
        return None
    return asyncio.run_coroutine_threadsafe(
        _disconnect_client(connection_ref, client, ping_task),
        _LOOP,
    )


def start_feishu_long_connection_clients() -> int:
    try:
        import lark_oapi as lark
    except Exception as exc:
        logger.error(f"lark-oapi is not installed: {exc}")
        with _LOCK:
            _LAST_ERRORS[0] = f"lark-oapi is not installed: {exc}"
        return 0

    loop = _ensure_lark_loop()
    desired: Dict[str, Tuple[int, str, str]] = {}
    with Session(engine) as session:
        rows = session.exec(select(BotConnection).where(
            BotConnection.channel == "feishu", BotConnection.enabled.is_(True),
            BotConnection.state != "deleted",
        )).all()
    for row in rows:
        config_id = int(row.ai_config_id)
        bot_cfg = connection_config(row, FEISHU_DEFAULTS)
        app_id = str(bot_cfg.get("app_id") or "").strip()
        app_secret = str(bot_cfg.get("app_secret") or "").strip()
        if config_id and bot_cfg.get("enabled") and app_id and app_secret:
            desired[row.connection_ref] = (config_id, app_id, app_secret)

    disconnects = []
    with _LOCK:
        active_ids = set(_CLIENTS.keys()) | set(_STARTING_CONFIG_IDS)
        for connection_ref in active_ids:
            signature = desired.get(connection_ref)
            wanted = signature[1:] if signature else None
            if wanted != _SIGNATURES.get(connection_ref):
                future = _schedule_disconnect_locked(connection_ref)
                if future is not None:
                    disconnects.append(future)

    for future in disconnects:
        try:
            future.result(timeout=5)
        except Exception:
            logger.exception("disconnect wait failed")

    started = 0
    for connection_ref, (config_id, app_id, app_secret) in desired.items():
        with _LOCK:
            if connection_ref in _CLIENTS or connection_ref in _STARTING_CONFIG_IDS:
                continue
            _STARTING_CONFIG_IDS.add(connection_ref)
            _SIGNATURES[connection_ref] = (app_id, app_secret)
            _CONFIG_IDS[connection_ref] = config_id
            _LAST_ERRORS.pop(connection_ref, None)
        try:
            client = _build_client(lark, config_id, connection_ref, app_id, app_secret)
            with _LOCK:
                _CLIENTS[connection_ref] = client
            asyncio.run_coroutine_threadsafe(_connect_client(connection_ref, client), loop)
            started += 1
        except Exception as exc:
            logger.exception(f"start failed config_id={config_id}")
            with _LOCK:
                _CLIENTS.pop(connection_ref, None)
                _PING_TASKS.pop(connection_ref, None)
                _SIGNATURES.pop(connection_ref, None)
                _STARTING_CONFIG_IDS.discard(connection_ref)
                _LAST_ERRORS[connection_ref] = str(exc)
    return started


def get_feishu_long_connection_state(config_id: int, connection_ref: str = "") -> Dict[str, str]:
    with _LOCK:
        refs = [connection_ref] if connection_ref else [ref for ref, cid in _CONFIG_IDS.items() if cid == config_id]
        is_connected = any(
            _CLIENTS.get(ref) is not None and getattr(_CLIENTS.get(ref), "_conn", None) is not None
            for ref in refs
        )
        is_starting = any(ref in _STARTING_CONFIG_IDS for ref in refs)
        error = next((_LAST_ERRORS.get(ref, "") for ref in refs if _LAST_ERRORS.get(ref)), "")
    if is_connected:
        return {"status": "success", "message": "长连接运行中"}
    if is_starting:
        return {"status": "success", "message": "长连接启动中"}
    if error:
        return {"status": "failed", "message": error}
    return {"status": "failed", "message": "长连接未运行"}
