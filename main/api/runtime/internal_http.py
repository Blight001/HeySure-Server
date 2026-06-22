"""Shared primitives for ``/internal/*`` HTTP endpoints.

Provides:
- ``require_internal_token`` — FastAPI dependency that checks the Bearer
  token against ``INTERNAL_TOKEN``. When the env is empty (monolith /
  local dev) the dependency only allows loopback callers; when set, the
  token must match.
- ``internal_client`` — small httpx wrapper that injects the Bearer
  header and a default timeout. Used by api-gateway / ai-runtime to call
  mcp-runtime / connector-runtime.
"""

from __future__ import annotations

import asyncio
import threading
from typing import Any, Dict, Optional

from fastapi import Header, HTTPException, Request

from ..core.config import INTERNAL_TOKEN


def require_internal_token(
    request: Request,
    authorization: Optional[str] = Header(None),
) -> None:
    """FastAPI dependency: gate ``/internal/*`` on a shared Bearer secret.

    Behavior:
    - If ``INTERNAL_TOKEN`` is set: ``Authorization: Bearer <token>`` must
      match. Mismatch / missing → 401.
    - If ``INTERNAL_TOKEN`` is empty: only loopback (127.0.0.1, ::1) clients
      may reach this endpoint. Anything else → 401. This keeps the monolith
      deployment safe even when the env isn't configured.
    """
    if INTERNAL_TOKEN:
        if not authorization or not authorization.lower().startswith("bearer "):
            raise HTTPException(status_code=401, detail="Internal token required")
        token = authorization.split(" ", 1)[1].strip()
        if token != INTERNAL_TOKEN:
            raise HTTPException(status_code=401, detail="Invalid internal token")
        return

    # Token not configured — limit to loopback.
    client = request.client
    host = client.host if client else ""
    if host not in ("127.0.0.1", "::1", "localhost"):
        raise HTTPException(
            status_code=401,
            detail="Internal endpoint requires HEYSURE_INTERNAL_TOKEN when called from non-loopback",
        )


def internal_headers() -> Dict[str, str]:
    """Headers to attach when calling another service's ``/internal/*``."""
    if not INTERNAL_TOKEN:
        return {}
    return {"Authorization": f"Bearer {INTERNAL_TOKEN}"}


# Sync httpx clients are safe across ``asyncio.run()`` / worker-thread bridges.
# Async clients are not — they bind to the first loop that created them.
_sync_clients: Dict[str, Any] = {}
_sync_client_lock = threading.Lock()


def _get_internal_sync_client(base_url: str, timeout: float = 120.0):
    import httpx  # local import keeps httpx optional for the monolith path

    key = f"{base_url.rstrip('/')}|{timeout}"
    with _sync_client_lock:
        client = _sync_clients.get(key)
        if client is None or client.is_closed:
            client = httpx.Client(
                base_url=base_url.rstrip("/"),
                timeout=timeout,
                headers=internal_headers(),
            )
            _sync_clients[key] = client
        return client


async def internal_post(
    base_url: str,
    path: str,
    *,
    json: Optional[Dict[str, Any]] = None,
    timeout: float = 120.0,
) -> Any:
    """POST to another service's ``/internal/*`` endpoint without loop coupling."""
    client = _get_internal_sync_client(base_url, timeout)
    try:
        response = await asyncio.to_thread(client.post, path, json=json or {})
    except RuntimeError as exc:
        if "Event loop is closed" in str(exc):
            # Heal: recreate client (in case) and let caller retry via bridge heal.
            # Re-raise so the call site sees the transient; next run_async will use fresh loop.
            if client.is_closed:
                key = f"{base_url.rstrip('/')}|{timeout}"
                with _sync_client_lock:
                    _sync_clients.pop(key, None)
            raise
        raise
    response.raise_for_status()
    return response.json()


def get_internal_async_client(base_url: str, timeout: float = 120.0):
    """Deprecated alias kept for older call sites; returns the sync client."""
    return _get_internal_sync_client(base_url, timeout)


class InternalClient:
    """Thin httpx client wrapper for cross-service calls.

    Lazily imports httpx so this module stays importable even when only the
    in-process monolith is in use.
    """

    def __init__(self, base_url: str, timeout: float = 30.0) -> None:
        if not base_url:
            raise ValueError("base_url required")
        self._base = base_url.rstrip("/")
        self._timeout = timeout
        self._client = None  # type: ignore[assignment]

    def _ensure_client(self):
        if self._client is None:
            import httpx  # local import keeps the dependency optional
            self._client = httpx.Client(
                base_url=self._base,
                timeout=self._timeout,
                headers=internal_headers(),
            )
        return self._client

    def post(self, path: str, json: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        client = self._ensure_client()
        resp = client.post(path, json=json or {})
        resp.raise_for_status()
        return resp.json()

    def get(self, path: str, params: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        client = self._ensure_client()
        resp = client.get(path, params=params or {})
        resp.raise_for_status()
        return resp.json()

    def close(self) -> None:
        if self._client is not None:
            self._client.close()
            self._client = None
