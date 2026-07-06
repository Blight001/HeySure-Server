"""Human-driven interactive remote terminal (命令行远程 / PTY over Socket.IO).

This is the **second remote-connection data plane**, a sibling of the WebRTC
screen mirror in ``remote_control.py``. Where screen-remote pushes ~30fps video
peer-to-peer (and therefore needs STUN/TURN to punch through NAT), a terminal is
just a low-bandwidth byte stream — so it rides the **Socket.IO relay itself**.
The bytes hop controller ↔ server ↔ device over the same agent socket that
carries registration and task dispatch; there is no WebRTC peer, no SDP/ICE, and
crucially **no TURN dependency** — the terminal works across the public internet
in exactly the setups where screen-remote can't. See device/read.md「统一远程连接」.

A live operator opens the web console, clicks a desktop device, and gets a real
shell: keystrokes flow browser → device PTY, PTY output (ANSI, cursor moves, TUI
apps) flows device → browser. None of it is persisted, queued, or routed through
the chat pipeline — those exist for AI tool calls and are far too heavy for an
interactive keystroke/output loop.

Signaling + data protocol (event names shared by both ends; payloads carry
``sessionId``; ``data`` is base64 of the raw terminal bytes so control sequences
survive intact):

    controller (web)  → server → device
        rt:open       {deviceId, token, shell?, cols?, rows?, cwd?}   open a session
        rt:input      {sessionId, data}     keystrokes into the PTY (base64 bytes)
        rt:resize     {sessionId, cols, rows}   window resize
        rt:close      {sessionId}           tear down

    device            → server → controller
        rt:data       {sessionId, data}     PTY output (base64 bytes)
        rt:exit       {sessionId, code}     the shell process exited
        rt:error      {sessionId, code, message}

    server            → controller
        rt:opened     {sessionId, deviceId, shell}   session accepted
        rt:error      {code, message}                start refused

Ownership is enforced at ``rt:open``: the controller proves its identity with the
same user JWT the agent uses, and the target device must be a live agent owned by
that user that advertises the ``remote_terminal`` capability. Mirrors
``remote_control.start_session`` exactly so the two channels share one gate.
"""

from __future__ import annotations

import logging
import time
import uuid
from dataclasses import dataclass, field
from typing import Any, Dict, Optional

from api.sio import agents, sio, resolve_agent_user, is_agent_shared_secret


logger = logging.getLogger(__name__)

# Capability the device advertises in device:register to unlock this channel.
RT_CAPABILITY = "remote_terminal"

# Sessions with no activity past this are reaped so a dropped peer never leaks a
# live PTY (the shell keeps running/draining resources otherwise).
_SESSION_TTL_SECONDS = 60 * 60


@dataclass
class RtSession:
    session_id: str
    device_id: str
    user_id: int
    controller_sid: str
    device_sid: str
    created_at: float = field(default_factory=time.time)


# sessionId -> RtSession
_SESSIONS: Dict[str, RtSession] = {}


def _find_device_sid(device_id: str) -> Optional[str]:
    target = str(device_id or "").strip()
    if not target:
        return None
    for sid, agent in agents.items():
        if str(agent.get("id")) == target:
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
    return RT_CAPABILITY in caps


def _purge_expired(now: Optional[float] = None) -> None:
    now = now if now is not None else time.time()
    stale = [sid for sid, s in _SESSIONS.items() if now - s.created_at > _SESSION_TTL_SECONDS]
    for session_id in stale:
        _SESSIONS.pop(session_id, None)


def _resolve_controller_user(token: Any) -> Optional[int]:
    """Verify the controller's identity from its user JWT (or shared secret)."""
    raw = str(token or "").strip()
    if not raw:
        return None
    if is_agent_shared_secret(raw):
        # Shared-secret callers are server-trusted; ownership is re-checked
        # against the device's bound user below, so a user_id is still needed.
        return None
    resolved = resolve_agent_user(raw)
    return int(resolved[0]) if resolved else None


async def open_session(controller_sid: str, data: Dict[str, Any]) -> None:
    """Handle ``rt:open`` from the web console."""
    _purge_expired()
    data = data if isinstance(data, dict) else {}
    device_id = str(data.get("deviceId") or "").strip()
    if not device_id:
        await sio.emit("rt:error", {"code": "bad_request", "message": "deviceId required"}, to=controller_sid)
        return

    user_id = _resolve_controller_user(data.get("token"))
    if user_id is None:
        await sio.emit(
            "rt:error",
            {"code": "unauthorized", "message": "登录态无效，请重新登录后再发起命令行远程"},
            to=controller_sid,
        )
        return

    device_sid = _find_device_sid(device_id)
    if not device_sid:
        await sio.emit(
            "rt:error",
            {"code": "offline", "message": "目标设备不在线"},
            to=controller_sid,
        )
        return

    if _agent_owner(device_sid) != user_id:
        await sio.emit(
            "rt:error",
            {"code": "forbidden", "message": "无权控制该设备"},
            to=controller_sid,
        )
        return

    if not _agent_supports_rt(device_sid):
        await sio.emit(
            "rt:error",
            {"code": "unsupported", "message": "该设备版本不支持命令行远程（请更新端侧客户端后重连）"},
            to=controller_sid,
        )
        return

    session_id = f"rt_{uuid.uuid4().hex[:12]}"
    _SESSIONS[session_id] = RtSession(
        session_id=session_id,
        device_id=device_id,
        user_id=user_id,
        controller_sid=controller_sid,
        device_sid=device_sid,
    )
    logger.info("remote-terminal open session=%s device=%s user=%s", session_id, device_id, user_id)
    # Tell the device to spawn the PTY. Forward the requested shell/geometry/cwd
    # verbatim — the device is the authority on what it can honor.
    await sio.emit(
        "rt:open",
        {
            "sessionId": session_id,
            "shell": data.get("shell"),
            "cols": data.get("cols"),
            "rows": data.get("rows"),
            "cwd": data.get("cwd"),
        },
        to=device_sid,
    )
    # Ack the controller so it can wire up its terminal and start sending input.
    await sio.emit(
        "rt:opened",
        {"sessionId": session_id, "deviceId": device_id, "shell": data.get("shell")},
        to=controller_sid,
    )


_TERMINAL_EVENTS = ("rt:close", "rt:exit", "rt:error")


async def relay(sid: str, event: str, data: Dict[str, Any]) -> None:
    """Forward one terminal message to the *other* peer of its session.

    Direction-agnostic (like ``remote_control.relay``): the sender is matched
    against the session's controller / device socket and the payload is delivered
    to whichever side it is not. Terminal events also drop the session so a closed
    terminal frees the device's PTY.
    """
    data = data if isinstance(data, dict) else {}
    session = _SESSIONS.get(str(data.get("sessionId") or ""))
    if not session:
        return
    if sid == session.controller_sid:
        target = session.device_sid
    elif sid == session.device_sid:
        target = session.controller_sid
    else:
        return  # sid is not a member of this session — ignore (spoofing guard)
    payload = dict(data)
    payload["sessionId"] = session.session_id
    await sio.emit(event, payload, to=target)
    if event in _TERMINAL_EVENTS:
        _SESSIONS.pop(session.session_id, None)
        logger.info("remote-terminal end (%s) session=%s", event, session.session_id)


async def handle_disconnect(sid: str) -> None:
    """Tear down any session whose controller or device socket dropped."""
    for session in [s for s in _SESSIONS.values() if s.controller_sid == sid or s.device_sid == sid]:
        _SESSIONS.pop(session.session_id, None)
        if session.controller_sid == sid:
            # Operator closed the tab — tell the device to kill the PTY.
            await sio.emit("rt:close", {"sessionId": session.session_id}, to=session.device_sid)
        else:
            # Device dropped — tell the operator the terminal ended.
            await sio.emit(
                "rt:exit",
                {"sessionId": session.session_id, "code": None, "reason": "device_disconnected"},
                to=session.controller_sid,
            )
        logger.info("remote-terminal cleanup on disconnect session=%s sid=%s", session.session_id, sid)
