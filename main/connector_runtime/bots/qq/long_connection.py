import asyncio
import logging
import threading
from typing import Any, Dict, Optional, Tuple

from sqlmodel import Session, select

from api.database import engine
from api.models import BotConnection
from api.services.bot_directory import connection_config
from ._config import QQ_DEFAULTS


logger = logging.getLogger(__name__)

_LOCK = threading.Lock()
_CLIENTS: Dict[str, Any] = {}
_TASKS: Dict[str, asyncio.Task] = {}
_LOOPS: Dict[str, asyncio.AbstractEventLoop] = {}
_THREADS: Dict[str, threading.Thread] = {}
_SIGNATURES: Dict[str, Tuple[str, str, bool]] = {}
_STARTING_CONFIG_IDS: set[str] = set()
_READY_CONFIG_IDS: set[str] = set()
_LAST_ERRORS: Dict[str, str] = {}
_CONFIG_IDS: Dict[str, int] = {}


def _build_message_payload(event_type: str, message: Any) -> Dict[str, Any]:
    def _compact_user(value: Any) -> Dict[str, Any]:
        payload: Dict[str, Any] = {}
        if value is None:
            return payload
        for key in ("id", "username", "avatar", "bot", "user_openid", "member_openid"):
            try:
                raw = getattr(value, key, None)
            except Exception:
                raw = None
            if raw is not None:
                payload[key] = raw
        return payload

    def _compact_member(value: Any) -> Dict[str, Any]:
        payload: Dict[str, Any] = {}
        if value is None:
            return payload
        for key in ("nick", "roles", "joined_at"):
            try:
                raw = getattr(value, key, None)
            except Exception:
                raw = None
            if raw is not None:
                payload[key] = raw
        return payload

    def _compact_message_ref(value: Any) -> Dict[str, Any]:
        payload: Dict[str, Any] = {}
        if value is None:
            return payload
        try:
            raw = getattr(value, "message_id", None)
        except Exception:
            raw = None
        if raw is not None:
            payload["message_id"] = raw
        return payload

    author = _compact_user(getattr(message, "author", None))
    payload: Dict[str, Any] = {
        "op": 0,
        "id": str(getattr(message, "event_id", "") or getattr(message, "id", "") or ""),
        "t": event_type,
        "d": {
            "id": str(getattr(message, "id", "") or ""),
            "content": str(getattr(message, "content", "") or ""),
            "channel_id": str(getattr(message, "channel_id", "") or ""),
            "guild_id": str(getattr(message, "guild_id", "") or ""),
            "group_openid": str(getattr(message, "group_openid", "") or ""),
            "group_id": str(getattr(message, "group_openid", "") or ""),
            "src_guild_id": str(getattr(message, "src_guild_id", "") or ""),
            "direct_message": getattr(message, "direct_message", None),
            "author": author,
            "author_id": author.get("user_openid") or author.get("member_openid") or author.get("id") or "",
            "member": _compact_member(getattr(message, "member", None)),
            "message_reference": _compact_message_ref(getattr(message, "message_reference", None)),
            "seq": getattr(message, "seq", None),
            "seq_in_channel": getattr(message, "seq_in_channel", None),
            "msg_seq": getattr(message, "msg_seq", None),
            "timestamp": getattr(message, "timestamp", None),
            "event_id": str(getattr(message, "event_id", "") or ""),
        },
    }

    attachments = []
    for item in getattr(message, "attachments", []) or []:
        attachment: Dict[str, Any] = {}
        for key in ("content_type", "filename", "height", "width", "id", "size", "url"):
            try:
                raw = getattr(item, key, None)
            except Exception:
                raw = None
            if raw is not None:
                attachment[key] = raw
        if attachment:
            attachments.append(attachment)
    if attachments:
        payload["d"]["attachments"] = attachments

    mentions = []
    for item in getattr(message, "mentions", []) or []:
        mention = _compact_user(item)
        if mention:
            mentions.append(mention)
    if mentions:
        payload["d"]["mentions"] = mentions

    return payload


def _mark_ready(runtime_key: str) -> None:
    with _LOCK:
        _READY_CONFIG_IDS.add(runtime_key)
        _STARTING_CONFIG_IDS.discard(runtime_key)
        _LAST_ERRORS.pop(runtime_key, None)


def _mark_error(runtime_key: str, exc: BaseException) -> None:
    with _LOCK:
        _LAST_ERRORS[runtime_key] = str(exc)
        _READY_CONFIG_IDS.discard(runtime_key)
        _STARTING_CONFIG_IDS.discard(runtime_key)


async def _dispatch_botpy_event(config_id: int, connection_ref: str, payload: Dict[str, Any]) -> None:
    from .router import handle_qq_event_payload

    await asyncio.to_thread(handle_qq_event_payload, int(config_id), payload, connection_ref=connection_ref)


def _build_client(botpy, config_id: int, connection_ref: str, *, app_id: str, app_secret: str, is_sandbox: bool):
    class QQLongConnectionClient(botpy.Client):
        def __init__(self):
            intents = botpy.Intents.default()
            super().__init__(
                intents=intents,
                is_sandbox=is_sandbox,
                bot_log=False,
            )
            self._config_id = int(config_id)
            self._runtime_key = connection_ref

        async def on_ready(self):
            robot_name = ""
            try:
                robot_name = str(getattr(self.robot, "name", "") or "")
            except Exception:
                robot_name = ""
            _mark_ready(self._runtime_key)
            logger.info(
                f"ready config_id={self._config_id}"
                + (f" robot={robot_name}" if robot_name else "")
            )

        async def _dispatch(self, event_type: str, message: Any) -> None:
            payload = _build_message_payload(event_type, message)
            try:
                await _dispatch_botpy_event(self._config_id, self._runtime_key, payload)
            except Exception:
                logger.exception(
                    f"event failed config_id={self._config_id} event_type={event_type}"
                )

        async def on_at_message_create(self, message: Any):
            await self._dispatch("AT_MESSAGE_CREATE", message)

        async def on_group_at_message_create(self, message: Any):
            await self._dispatch("GROUP_AT_MESSAGE_CREATE", message)

        async def on_c2c_message_create(self, message: Any):
            await self._dispatch("C2C_MESSAGE_CREATE", message)

        async def on_direct_message_create(self, message: Any):
            await self._dispatch("DIRECT_MESSAGE_CREATE", message)

        async def on_message_create(self, message: Any):
            try:
                snapshot = {
                    "id": getattr(message, "id", None),
                    "channel_id": getattr(message, "channel_id", None),
                    "guild_id": getattr(message, "guild_id", None),
                    "content": getattr(message, "content", None),
                }
                logger.debug(
                    f"message_create config_id={self._config_id} {snapshot}"
                )
            except Exception:
                logger.debug(f"message_create config_id={self._config_id}")

    return QQLongConnectionClient()


def _thread_main(runtime_key: str, config_id: int, app_id: str, app_secret: str, is_sandbox: bool, ready_event: threading.Event) -> None:
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    try:
        import botpy
    except Exception as exc:
        _mark_error(runtime_key, exc)
        ready_event.set()
        logger.exception(f"botpy import failed config_id={config_id}")
        loop.close()
        return

    try:
        client = _build_client(
            botpy,
            config_id,
            runtime_key,
            app_id=app_id,
            app_secret=app_secret,
            is_sandbox=is_sandbox,
        )
        task = loop.create_task(client.start(app_id, app_secret))

        def _on_done(fut: asyncio.Future) -> None:
            exc: Optional[BaseException]
            try:
                exc = fut.exception()
            except asyncio.CancelledError:
                exc = None
            except Exception as inner_exc:
                exc = inner_exc
            with _LOCK:
                if _TASKS.get(runtime_key) is fut:
                    _TASKS.pop(runtime_key, None)
                    _CLIENTS.pop(runtime_key, None)
                    _LOOPS.pop(runtime_key, None)
                    _THREADS.pop(runtime_key, None)
                    _READY_CONFIG_IDS.discard(runtime_key)
                    _STARTING_CONFIG_IDS.discard(runtime_key)
                    if exc is not None:
                        _LAST_ERRORS[runtime_key] = str(exc)
            try:
                loop.stop()
            except Exception:
                pass

        task.add_done_callback(_on_done)
        with _LOCK:
            _CLIENTS[runtime_key] = client
            _TASKS[runtime_key] = task
            _LOOPS[runtime_key] = loop
            _THREADS[runtime_key] = threading.current_thread()
        ready_event.set()
        loop.run_forever()
    except Exception as exc:
        _mark_error(runtime_key, exc)
        ready_event.set()
        logger.exception(f"start failed config_id={config_id}")
    finally:
        pending = [task for task in asyncio.all_tasks(loop) if not task.done()]
        for pending_task in pending:
            pending_task.cancel()
        if pending:
            try:
                loop.run_until_complete(asyncio.gather(*pending, return_exceptions=True))
            except Exception:
                pass
        try:
            loop.close()
        except Exception:
            pass


def _schedule_disconnect_locked(runtime_key: str) -> None:
    task = _TASKS.pop(runtime_key, None)
    loop = _LOOPS.pop(runtime_key, None)
    _CLIENTS.pop(runtime_key, None)
    _THREADS.pop(runtime_key, None)
    _READY_CONFIG_IDS.discard(runtime_key)
    _STARTING_CONFIG_IDS.discard(runtime_key)
    _LAST_ERRORS.pop(runtime_key, None)
    _CONFIG_IDS.pop(runtime_key, None)
    if loop is None:
        return
    try:
        if task is not None:
            loop.call_soon_threadsafe(task.cancel)
        loop.call_soon_threadsafe(loop.stop)
    except Exception:
        pass


def start_qq_long_connection_clients() -> int:
    try:
        pass
    except Exception as exc:
        logger.error(f"botpy is not installed: {exc}")
        with _LOCK:
            _LAST_ERRORS[0] = f"botpy is not installed: {exc}"
        return 0

    desired: Dict[str, Tuple[int, str, str, bool]] = {}
    with Session(engine) as session:
        rows = session.exec(select(BotConnection).where(
            BotConnection.channel == "qq", BotConnection.enabled.is_(True),
            BotConnection.state != "deleted",
        )).all()
    for row in rows:
        config_id = int(row.ai_config_id)
        bot_cfg = connection_config(row, QQ_DEFAULTS)
        app_id = str(bot_cfg.get("app_id") or "").strip()
        app_secret = str(bot_cfg.get("app_secret") or "").strip()
        if (
            config_id
            and bot_cfg.get("enabled")
            and app_id
            and app_secret
        ):
            desired[row.connection_ref] = (
                config_id, app_id, app_secret, bool(bot_cfg.get("sandbox"))
            )

    if desired:
        logger.debug(f"desired_configs={sorted(desired.keys())}")

    with _LOCK:
        active_ids = set(_CLIENTS.keys()) | set(_STARTING_CONFIG_IDS)
        for runtime_key in active_ids:
            values = desired.get(runtime_key)
            wanted = values[1:] if values else None
            if wanted != _SIGNATURES.get(runtime_key):
                _schedule_disconnect_locked(runtime_key)

    started = 0
    for runtime_key, (config_id, app_id, app_secret, is_sandbox) in desired.items():
        with _LOCK:
            if runtime_key in _CLIENTS or runtime_key in _STARTING_CONFIG_IDS:
                continue
            _STARTING_CONFIG_IDS.add(runtime_key)
            _SIGNATURES[runtime_key] = (app_id, app_secret, is_sandbox)
            _CONFIG_IDS[runtime_key] = config_id
            _LAST_ERRORS.pop(runtime_key, None)

        ready_event = threading.Event()
        thread = threading.Thread(
            target=_thread_main,
            args=(runtime_key, config_id, app_id, app_secret, is_sandbox, ready_event),
            name=f"qq-ws-{runtime_key}",
            daemon=True,
        )
        thread.start()
        with _LOCK:
            _THREADS[runtime_key] = thread
        ready_event.wait(timeout=5)
        started += 1
    if started:
        logger.info(f"started={started}")
    return started


def get_qq_long_connection_state(config_id: int, connection_ref: str = "") -> Dict[str, str]:
    with _LOCK:
        refs = [connection_ref] if connection_ref else [ref for ref, cid in _CONFIG_IDS.items() if cid == config_id]
        ready_ref = next((ref for ref in refs if ref in _READY_CONFIG_IDS), "")
        thread = _THREADS.get(ready_ref)
        is_starting = any(ref in _STARTING_CONFIG_IDS for ref in refs)
        error = next((_LAST_ERRORS.get(ref, "") for ref in refs if _LAST_ERRORS.get(ref)), "")
        client = _CLIENTS.get(ready_ref)
        task = _TASKS.get(ready_ref)
    if ready_ref and thread and thread.is_alive() and client is not None and task is not None:
        return {"status": "success", "message": "botpy 长连接运行中"}
    if is_starting:
        return {"status": "success", "message": "botpy 长连接启动中"}
    if error:
        return {"status": "failed", "message": error}
    return {"status": "failed", "message": "botpy 长连接未运行"}
