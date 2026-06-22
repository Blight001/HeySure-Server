import asyncio
import logging
import threading
from typing import Optional, Tuple

import socketio
from sqlmodel import Session, select

from .auth import decode_access_token
from .core.settings import settings
from .database import engine


logger = logging.getLogger(__name__)


# Process role decides whether this process owns a real Socket.IO server or
# is a "leaf" that needs to forward emits to the api-gateway over HTTP.
#   gateway          — full Socket.IO server (default; current monolith)
#   worker | mcp     — no server, emits forwarded via api_gateway_url
#   connector        — full Socket.IO server for /agent namespace (Phase 4)
HEYSURE_SERVICE_ROLE = settings.service_role
_HAS_LOCAL_SIO_SERVER = HEYSURE_SERVICE_ROLE in ("gateway", "connector")


class _RemoteSio:
    """Stand-in for ``socketio.AsyncServer`` used in ai-runtime / mcp-runtime.

    Forwards ``.emit(...)`` over HTTP to the api-gateway's
    ``/internal/socket/emit`` so existing call sites that do
    ``await sio.emit(...)`` keep working unchanged.

    Other server methods (``enter_room``, ``on``, etc.) collapse to no-ops
    because workers do not accept Socket.IO connections themselves.
    """

    def __init__(self, gateway_url: str) -> None:
        self._gateway_url = (gateway_url or "").rstrip("/")
        self._client = None
        self._client_lock = threading.Lock()

    def _ensure_client(self):
        if self._client is None:
            with self._client_lock:
                if self._client is None:
                    import httpx  # local import — only needed in split mode
                    from .runtime.internal_http import internal_headers
                    # Live updates are often emitted from synchronous worker
                    # threads via a fresh ``asyncio.run`` call. A cached
                    # AsyncClient becomes bound to the first short-lived event
                    # loop and fails on later updates after that loop closes.
                    self._client = httpx.Client(
                        base_url=self._gateway_url,
                        headers=internal_headers(),
                        timeout=10.0,
                    )
        return self._client

    async def emit(self, event, data=None, to=None, room=None, namespace=None, **_):
        if not self._gateway_url:
            # No gateway configured (likely misconfiguration). Drop silently
            # rather than crash the worker over a UI nicety.
            return
        try:
            client = self._ensure_client()
            response = await asyncio.to_thread(
                client.post,
                "/internal/socket/emit",
                json={
                    "event": event,
                    "data": data,
                    "to": to,
                    "room": room,
                    "namespace": namespace,
                },
            )
            response.raise_for_status()
        except RuntimeError as exc:
            if "Event loop is closed" in str(exc):
                return
            logger.exception(f"sio-proxy forward failed event={event}")
        except Exception:
            # Logging only — emit failures should never break the worker.
            logger.exception(f"sio-proxy forward failed event={event}")

    async def enter_room(self, *_, **__):
        return None

    async def leave_room(self, *_, **__):
        return None

    def on(self, *_, **__):
        # Returns a decorator no-op so registration calls remain side-effect free.
        def _decorator(fn):
            return fn
        return _decorator


if _HAS_LOCAL_SIO_SERVER:
    # Browser screenshots are transported as base64 data URLs before the
    # server persists them to the workspace. Complex pages can exceed
    # Socket.IO's small default payload limit, which makes the agent result
    # disappear and leaves the caller waiting until the dispatch timeout.
    sio = socketio.AsyncServer(
        async_mode='asgi',
        cors_allowed_origins='*',
        max_http_buffer_size=20_000_000,
    )
else:
    sio = _RemoteSio(settings.api_gateway_url)

# Connected desktop/browser agents: sid -> agent info dict.
agents = {}


def device_token_required() -> bool:
    # Agent registration always requires a logged-in user JWT now.
    # The AGENT_TOKEN env var is an optional shared-secret bypass for
    # server-trusted internal agents.
    return True


def is_agent_shared_secret(token: str) -> bool:
    """Return True if ``token`` matches the shared AGENT_TOKEN env var.

    Empty AGENT_TOKEN disables this bypass (the default in dev).
    """
    if not settings.agent_token:
        return False
    return str(token or "").strip() == settings.agent_token


def resolve_agent_user(token: str) -> Optional[Tuple[int, str]]:
    """Validate a token as a user JWT and return ``(user_id, account)``.

    The agent client sends the same user JWT it received from ``/api/auth/login``
    as its ``token``. We decode it here so the server can refuse registration
    for agents whose user hasn't logged in (or whose token has expired).
    Returns ``None`` if the token is missing, malformed, expired, or maps to
    no user.
    """
    raw = str(token or "").strip()
    if not raw:
        return None
    if raw.startswith("Bearer "):
        raw = raw.split(" ", 1)[1].strip()
    payload = decode_access_token(raw)
    if not payload:
        return None
    account = payload.get("sub")
    if not account:
        return None
    # Lazy import to avoid a circular dependency between sio and models.
    from .models import User

    with Session(engine) as session:
        user = session.exec(select(User).where(User.account == account)).first()
        if user is None:
            return None
        return int(user.id), str(user.account)


def is_agent_token_valid(token: str) -> bool:
    """Backwards-compatible boolean variant of the new auth check."""
    if is_agent_shared_secret(token):
        return True
    return resolve_agent_user(token) is not None
