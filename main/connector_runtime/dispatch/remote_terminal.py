"""Human-operated PTY byte relay over the endpoint Socket.IO connection.

The server never interprets terminal contents and never persists them. It
authenticates the controller, verifies device ownership/capability, bounds each
message, and relays only the events allowed for that peer direction.

Protocol (``data`` is strict base64 of raw PTY bytes):

* controller -> device: ``rt:open``, ``rt:input``, ``rt:resize``, ``rt:close``
* device -> controller: ``rt:data``, ``rt:ready`` (optional), ``rt:exit``,
  ``rt:error``
* server -> controller: ``rt:opened`` and validation/authentication errors

``rt:opened`` retains its historical meaning: the server accepted and
forwarded the request. New devices may additionally send ``rt:ready`` after
the PTY has actually spawned; older devices remain compatible.
"""

from __future__ import annotations

import asyncio
import base64
import binascii
import logging
import time
import uuid
from dataclasses import dataclass, field
from typing import Any, Dict, Optional

from api.sio import agents, is_agent_shared_secret, resolve_agent_user, sio


logger = logging.getLogger(__name__)

RT_CAPABILITY = "remote_terminal"
_SESSION_TTL_SECONDS = 60 * 60
_REAPER_INTERVAL_SECONDS = 30
_MAX_SESSIONS_PER_USER = 4
_MAX_SESSIONS_PER_DEVICE = 2
_MAX_DEVICE_ID_LENGTH = 256
_MAX_SHELL_LENGTH = 128
_MAX_CWD_LENGTH = 2048
_MAX_ERROR_CODE_LENGTH = 64
_MAX_MESSAGE_LENGTH = 512
_MAX_REASON_LENGTH = 128
_MAX_TERMINAL_BYTES = 256 * 1024
_MAX_BASE64_LENGTH = ((_MAX_TERMINAL_BYTES + 2) // 3) * 4
_MIN_COLS = 2
_MAX_COLS = 500
_MIN_ROWS = 1
_MAX_ROWS = 300

_CONTROLLER_EVENTS = frozenset({"rt:input", "rt:resize", "rt:close"})
_DEVICE_EVENTS = frozenset({"rt:data", "rt:ready", "rt:exit", "rt:error"})
_TERMINAL_EVENTS = frozenset({"rt:close", "rt:exit", "rt:error"})


@dataclass
class RtSession:
    session_id: str
    device_id: str
    user_id: int
    controller_sid: str
    device_sid: str
    created_at: float = field(default_factory=time.time)
    last_activity: float = field(default_factory=time.time)


@dataclass(frozen=True)
class OpenRequest:
    device_id: str
    shell: Optional[str]
    cols: Optional[int]
    rows: Optional[int]
    cwd: Optional[str]


_SESSIONS: Dict[str, RtSession] = {}
_REAPER_TASK: Optional[asyncio.Task[None]] = None


def _find_device_sid(device_id: str) -> Optional[str]:
    for sid, agent in agents.items():
        if str(agent.get("id")) == device_id:
            return sid
    return None


def _agent_owner(sid: str) -> Optional[int]:
    agent = agents.get(sid) or {}
    try:
        return int(agent.get("userId")) if agent.get("userId") is not None else None
    except (TypeError, ValueError):
        return None


def _agent_supports_rt(sid: str) -> bool:
    caps = (agents.get(sid) or {}).get("capabilities") or []
    return RT_CAPABILITY in caps or "remote.terminal" in caps


def _resolve_controller_user(token: Any) -> Optional[int]:
    raw = str(token or "").strip()
    if not raw or is_agent_shared_secret(raw):
        return None
    resolved = resolve_agent_user(raw)
    return int(resolved[0]) if resolved else None


def _optional_text(value: Any, maximum: int) -> Optional[str]:
    if value is None or value == "":
        return None
    if not isinstance(value, str):
        raise ValueError("must be a string")
    normalized = value.strip()
    if not normalized or len(normalized) > maximum:
        raise ValueError(f"must contain 1..{maximum} characters")
    return normalized


def _bounded_int(value: Any, minimum: int, maximum: int) -> Optional[int]:
    if value is None or value == "":
        return None
    if isinstance(value, bool):
        raise ValueError("must be an integer")
    try:
        parsed = int(value)
    except (TypeError, ValueError) as exc:
        raise ValueError("must be an integer") from exc
    if parsed < minimum or parsed > maximum:
        raise ValueError(f"must be between {minimum} and {maximum}")
    return parsed


def _required_bounded_int(value: Any, minimum: int, maximum: int) -> int:
    parsed = _bounded_int(value, minimum, maximum)
    if parsed is None:
        raise ValueError("must be an integer")
    return parsed


def _parse_open_request(data: Any) -> OpenRequest:
    if not isinstance(data, dict):
        raise ValueError("payload must be an object")
    raw_device_id = data.get("deviceId")
    if not isinstance(raw_device_id, str):
        raise ValueError("deviceId must be a string")
    device_id = raw_device_id.strip()
    if not device_id or len(device_id) > _MAX_DEVICE_ID_LENGTH:
        raise ValueError(f"deviceId must contain 1..{_MAX_DEVICE_ID_LENGTH} characters")
    return OpenRequest(
        device_id=device_id,
        shell=_optional_text(data.get("shell"), _MAX_SHELL_LENGTH),
        cols=_bounded_int(data.get("cols"), _MIN_COLS, _MAX_COLS),
        rows=_bounded_int(data.get("rows"), _MIN_ROWS, _MAX_ROWS),
        cwd=_optional_text(data.get("cwd"), _MAX_CWD_LENGTH),
    )


async def _emit_error(sid: str, code: str, message: str, session_id: str = "") -> None:
    payload = {"code": code, "message": message}
    if session_id:
        payload["sessionId"] = session_id
    await sio.emit("rt:error", payload, to=sid)


async def _safe_emit(event: str, payload: Dict[str, Any], target: str) -> None:
    try:
        await sio.emit(event, payload, to=target)
    except Exception:
        logger.exception("remote-terminal lifecycle notification failed event=%s", event)


async def _expire_session(session: RtSession) -> None:
    payload = {"sessionId": session.session_id, "reason": "idle_timeout"}
    await _safe_emit("rt:close", payload, session.device_sid)
    await _safe_emit("rt:exit", {**payload, "code": None}, session.controller_sid)
    logger.info("remote-terminal expired session=%s", session.session_id)


async def _purge_expired(now: Optional[float] = None) -> int:
    current = time.time() if now is None else now
    stale = [
        session
        for session in _SESSIONS.values()
        if current - session.last_activity > _SESSION_TTL_SECONDS
    ]
    expired = 0
    for session in stale:
        if _SESSIONS.pop(session.session_id, None) is not None:
            expired += 1
            await _expire_session(session)
    return expired


async def _reaper_loop() -> None:
    global _REAPER_TASK
    try:
        while _SESSIONS:
            await asyncio.sleep(_REAPER_INTERVAL_SECONDS)
            await _purge_expired()
    finally:
        _REAPER_TASK = None


def _ensure_reaper() -> None:
    global _REAPER_TASK
    if _REAPER_TASK is None or _REAPER_TASK.done():
        _REAPER_TASK = asyncio.create_task(_reaper_loop(), name="remote-terminal-reaper")


def _session_limit_error(user_id: int, device_id: str) -> Optional[str]:
    sessions = tuple(_SESSIONS.values())
    if sum(item.user_id == user_id for item in sessions) >= _MAX_SESSIONS_PER_USER:
        return "该账号的命令行远程会话数已达上限"
    if sum(item.device_id == device_id for item in sessions) >= _MAX_SESSIONS_PER_DEVICE:
        return "该设备的命令行远程会话数已达上限"
    return None


async def _authorize_open(
    controller_sid: str, request: OpenRequest, token: Any
) -> Optional[tuple[int, str]]:
    user_id = _resolve_controller_user(token)
    if user_id is None:
        await _emit_error(controller_sid, "unauthorized", "登录态无效，请重新登录后再发起命令行远程")
        return None
    device_sid = _find_device_sid(request.device_id)
    if not device_sid:
        await _emit_error(controller_sid, "offline", "目标设备不在线")
        return None
    if _agent_owner(device_sid) != user_id:
        await _emit_error(controller_sid, "forbidden", "无权控制该设备")
        return None
    if not _agent_supports_rt(device_sid):
        await _emit_error(controller_sid, "unsupported", "该设备版本不支持命令行远程（请更新端侧客户端后重连）")
        return None
    limit_error = _session_limit_error(user_id, request.device_id)
    if limit_error:
        await _emit_error(controller_sid, "session_limit", limit_error)
        return None
    return user_id, device_sid


async def open_session(controller_sid: str, data: Dict[str, Any]) -> None:
    """Validate, authorize, and forward ``rt:open`` from the web console."""
    await _purge_expired()
    try:
        request = _parse_open_request(data)
    except ValueError as exc:
        await _emit_error(controller_sid, "bad_request", str(exc))
        return
    authorized = await _authorize_open(controller_sid, request, data.get("token"))
    if authorized is None:
        return
    user_id, device_sid = authorized
    session_id = f"rt_{uuid.uuid4().hex[:12]}"
    _SESSIONS[session_id] = RtSession(
        session_id=session_id,
        device_id=request.device_id,
        user_id=user_id,
        controller_sid=controller_sid,
        device_sid=device_sid,
    )
    _ensure_reaper()
    logger.info("remote-terminal open session=%s device=%s user=%s", session_id, request.device_id, user_id)
    await sio.emit(
        "rt:open",
        {
            "sessionId": session_id,
            "shell": request.shell,
            "cols": request.cols,
            "rows": request.rows,
            "cwd": request.cwd,
        },
        to=device_sid,
    )
    await sio.emit(
        "rt:opened",
        {"sessionId": session_id, "deviceId": request.device_id, "shell": request.shell},
        to=controller_sid,
    )


def _valid_base64(value: Any) -> bool:
    if not isinstance(value, str) or len(value) > _MAX_BASE64_LENGTH:
        return False
    try:
        decoded = base64.b64decode(value, validate=True)
    except (binascii.Error, ValueError):
        return False
    return len(decoded) <= _MAX_TERMINAL_BYTES


def _bounded_message(value: Any, maximum: int, *, allow_none: bool = False) -> Optional[str]:
    if value is None and allow_none:
        return None
    if not isinstance(value, str) or len(value) > maximum:
        raise ValueError(f"text must be at most {maximum} characters")
    return value


def _normalize_relay_payload(event: str, data: Dict[str, Any], session_id: str) -> Dict[str, Any]:
    if event in {"rt:input", "rt:data"}:
        if not _valid_base64(data.get("data")):
            raise ValueError("data must be valid bounded base64")
        return {"sessionId": session_id, "data": data["data"]}
    if event == "rt:resize":
        return {
            "sessionId": session_id,
            "cols": _required_bounded_int(data.get("cols"), _MIN_COLS, _MAX_COLS),
            "rows": _required_bounded_int(data.get("rows"), _MIN_ROWS, _MAX_ROWS),
        }
    if event == "rt:ready":
        return {
            "sessionId": session_id,
            "shell": _optional_text(data.get("shell"), _MAX_SHELL_LENGTH),
            "cols": _bounded_int(data.get("cols"), _MIN_COLS, _MAX_COLS),
            "rows": _bounded_int(data.get("rows"), _MIN_ROWS, _MAX_ROWS),
        }
    if event == "rt:exit":
        code = data.get("code")
        if code is not None:
            code = _bounded_int(code, -(2**31), 2**31 - 1)
        return {
            "sessionId": session_id,
            "code": code,
            "reason": _bounded_message(data.get("reason"), _MAX_REASON_LENGTH, allow_none=True),
        }
    if event == "rt:error":
        return {
            "sessionId": session_id,
            "code": _bounded_message(data.get("code"), _MAX_ERROR_CODE_LENGTH),
            "message": _bounded_message(data.get("message"), _MAX_MESSAGE_LENGTH),
        }
    return {"sessionId": session_id}


def _relay_target(session: RtSession, sid: str, event: str) -> Optional[str]:
    if sid == session.controller_sid:
        return session.device_sid if event in _CONTROLLER_EVENTS else None
    if sid == session.device_sid:
        return session.controller_sid if event in _DEVICE_EVENTS else None
    return None


async def relay(sid: str, event: str, data: Dict[str, Any]) -> None:
    """Relay one validated message only in its declared protocol direction."""
    await _purge_expired()
    payload = data if isinstance(data, dict) else {}
    session_id = str(payload.get("sessionId") or "")
    session = _SESSIONS.get(session_id)
    if session is None:
        return
    target = _relay_target(session, sid, event)
    if target is None:
        if sid in {session.controller_sid, session.device_sid}:
            await _emit_error(sid, "invalid_direction", "该终端事件不允许由当前连接发送", session_id)
        return
    try:
        normalized = _normalize_relay_payload(event, payload, session_id)
    except ValueError as exc:
        await _emit_error(sid, "bad_payload", str(exc), session_id)
        return
    session.last_activity = time.time()
    await sio.emit(event, normalized, to=target)
    if event in _TERMINAL_EVENTS:
        _SESSIONS.pop(session_id, None)
        logger.info("remote-terminal end (%s) session=%s", event, session_id)


async def handle_disconnect(sid: str) -> None:
    """Notify the surviving peer and tear down every session for ``sid``."""
    await _purge_expired()
    sessions = [item for item in _SESSIONS.values() if sid in {item.controller_sid, item.device_sid}]
    for session in sessions:
        if _SESSIONS.pop(session.session_id, None) is None:
            continue
        if session.controller_sid == sid:
            await _safe_emit("rt:close", {"sessionId": session.session_id}, session.device_sid)
        else:
            await _safe_emit(
                "rt:exit",
                {"sessionId": session.session_id, "code": None, "reason": "device_disconnected"},
                session.controller_sid,
            )
        logger.info("remote-terminal cleanup on disconnect session=%s sid=%s", session.session_id, sid)
