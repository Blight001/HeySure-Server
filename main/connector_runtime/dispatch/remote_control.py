"""Human-driven interactive remote control (WebRTC screen mirroring + input).

This is a **separate data plane from the AI ``task:dispatch`` loop**. A live
operator opens the数字社会 (digital society) console, clicks an Android device,
and drives its screen in real time: video flows device → browser, taps/swipes
flow browser → device. None of it is persisted, queued, or routed through the
chat pipeline — those exist for AI tool calls and are far too heavy for a
~30fps stream plus high-frequency pointer events.

Topology (see ``gateway/routers/device_dispatch_internal.py``): both the web
console **and** the Android agent register their Socket.IO connection on the
same api-gateway server, so the live ``agents`` registry and the controller
socket live in one process. Signaling is therefore plain in-memory Socket.IO
relay — no internal-HTTP hop. The **media and input themselves never touch the
server**: they ride a peer-to-peer WebRTC connection (video track + a
``control`` DataChannel) negotiated through the small signaling messages below.

Signaling protocol (event names shared by both ends; payloads carry sessionId):

    controller (web)  → server → agent (android)
        rc:start      {deviceId, token}            open a session
        rc:answer     {sessionId, sdp}             SDP answer to the offer
        rc:ice        {sessionId, candidate}       trickle ICE
        rc:stop       {sessionId}                  tear down

    agent (android)   → server → controller (web)
        rc:offer      {sessionId, sdp}             SDP offer (android offers)
        rc:ice        {sessionId, candidate}       trickle ICE
        rc:ready      {sessionId, width, height, rotation}
        rc:error      {sessionId, code, message}
        rc:stopped    {sessionId}

    server            → controller
        rc:started    {sessionId, deviceId}        session accepted
        rc:error      {code, message}              start refused

Ownership is enforced at ``rc:start``: the controller proves its identity with
the same user JWT the agent uses, and the target device must be a live agent
owned by that user that advertises the ``remote_control`` capability.
"""

from __future__ import annotations

import logging
import time
import uuid
from dataclasses import dataclass, field
from typing import Any, Dict, Optional

from api.sio import agents, sio, resolve_agent_user, is_agent_shared_secret


logger = logging.getLogger(__name__)

# WebRTC capability the Android client advertises in device:register.
RC_CAPABILITY = "remote_control"

# Sessions with no activity past this are reaped so a dropped peer never leaks a
# half-open mirror (the device keeps capturing/draining battery otherwise).
_SESSION_TTL_SECONDS = 60 * 30


@dataclass
class RcSession:
    session_id: str
    device_id: str
    user_id: int
    controller_sid: str
    android_sid: str
    created_at: float = field(default_factory=time.time)


# sessionId -> RcSession
_SESSIONS: Dict[str, RcSession] = {}


def _find_android_sid(device_id: str) -> Optional[str]:
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


def _agent_supports_rc(sid: str) -> bool:
    caps = (agents.get(sid) or {}).get("capabilities") or []
    return RC_CAPABILITY in caps


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


async def start_session(controller_sid: str, data: Dict[str, Any]) -> None:
    """Handle ``rc:start`` from the web console."""
    _purge_expired()
    data = data if isinstance(data, dict) else {}
    device_id = str(data.get("deviceId") or "").strip()
    if not device_id:
        await sio.emit("rc:error", {"code": "bad_request", "message": "deviceId required"}, to=controller_sid)
        return

    user_id = _resolve_controller_user(data.get("token"))
    if user_id is None:
        await sio.emit(
            "rc:error",
            {"code": "unauthorized", "message": "登录态无效，请重新登录后再发起远程控制"},
            to=controller_sid,
        )
        return

    android_sid = _find_android_sid(device_id)
    if not android_sid:
        await sio.emit(
            "rc:error",
            {"code": "offline", "message": "目标安卓设备不在线"},
            to=controller_sid,
        )
        return

    if _agent_owner(android_sid) != user_id:
        await sio.emit(
            "rc:error",
            {"code": "forbidden", "message": "无权控制该设备"},
            to=controller_sid,
        )
        return

    if not _agent_supports_rc(android_sid):
        await sio.emit(
            "rc:error",
            {"code": "unsupported", "message": "该设备版本不支持远程控制（请更新端侧客户端后重连）"},
            to=controller_sid,
        )
        return

    session_id = f"rc_{uuid.uuid4().hex[:12]}"
    _SESSIONS[session_id] = RcSession(
        session_id=session_id,
        device_id=device_id,
        user_id=user_id,
        controller_sid=controller_sid,
        android_sid=android_sid,
    )
    logger.info("remote-control start session=%s device=%s user=%s", session_id, device_id, user_id)
    # Tell the device to bring up capture + the peer connection (it offers).
    await sio.emit("rc:start", {"sessionId": session_id}, to=android_sid)
    # Ack the controller so it can wire up its RTCPeerConnection and wait for
    # the offer.
    await sio.emit("rc:started", {"sessionId": session_id, "deviceId": device_id}, to=controller_sid)


_TERMINAL_EVENTS = ("rc:stop", "rc:stopped", "rc:error")


async def relay(sid: str, event: str, data: Dict[str, Any]) -> None:
    """Forward a signaling message to the *other* peer of its session.

    Direction-agnostic so the single ``rc:ice`` handler works both ways: the
    sender is matched against the session's controller / android socket and the
    payload is delivered to whichever side it is not. Terminal events also drop
    the session so a closed mirror frees the device.
    """
    data = data if isinstance(data, dict) else {}
    session = _SESSIONS.get(str(data.get("sessionId") or ""))
    if not session:
        return
    if sid == session.controller_sid:
        target = session.android_sid
    elif sid == session.android_sid:
        target = session.controller_sid
    else:
        return  # sid is not a member of this session — ignore (spoofing guard)
    payload = dict(data)
    payload["sessionId"] = session.session_id
    await sio.emit(event, payload, to=target)
    if event in _TERMINAL_EVENTS:
        _SESSIONS.pop(session.session_id, None)
        logger.info("remote-control end (%s) session=%s", event, session.session_id)


async def handle_disconnect(sid: str) -> None:
    """Tear down any session whose controller or device socket dropped."""
    for session in [s for s in _SESSIONS.values() if s.controller_sid == sid or s.android_sid == sid]:
        _SESSIONS.pop(session.session_id, None)
        if session.controller_sid == sid:
            # Operator closed the tab — tell the device to stop capturing.
            await sio.emit("rc:stop", {"sessionId": session.session_id}, to=session.android_sid)
        else:
            # Device dropped — tell the operator the mirror ended.
            await sio.emit(
                "rc:stopped",
                {"sessionId": session.session_id, "reason": "device_disconnected"},
                to=session.controller_sid,
            )
        logger.info("remote-control cleanup on disconnect session=%s sid=%s", session.session_id, sid)
