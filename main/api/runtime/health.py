"""Standard liveness, readiness, draining, and detail health contract."""

from __future__ import annotations

import os
import socket
import threading
import time
import uuid
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, Optional

from fastapi import APIRouter, HTTPException


DetailProvider = Callable[[], Dict[str, Any]]


def _instance_id() -> str:
    configured = os.getenv("HEYSURE_INSTANCE_ID", "").strip()
    if configured:
        return configured
    return f"{socket.gethostname()}-{os.getpid()}-{uuid.uuid4().hex[:8]}"


@dataclass
class RuntimeHealth:
    role: str
    instance_id: str = field(default_factory=_instance_id)
    started_at: float = field(default_factory=time.time)
    ready: bool = False
    draining: bool = False
    accepting_work: bool = False
    readiness_error: str = "starting"
    last_activity_at: Optional[float] = None
    _lock: threading.Lock = field(default_factory=threading.Lock, repr=False)

    def mark_ready(self, *, accepting_work: bool = True) -> None:
        with self._lock:
            self.ready = True
            self.draining = False
            self.accepting_work = accepting_work
            self.readiness_error = ""

    def mark_not_ready(self, reason: str) -> None:
        with self._lock:
            self.ready = False
            self.accepting_work = False
            self.readiness_error = str(reason or "not ready")

    def begin_draining(self) -> None:
        with self._lock:
            self.draining = True
            self.ready = False
            self.accepting_work = False
            self.readiness_error = "draining"

    def activity(self) -> None:
        with self._lock:
            self.last_activity_at = time.time()

    def snapshot(self) -> Dict[str, Any]:
        with self._lock:
            return {
                "service_role": self.role,
                "instance_id": self.instance_id,
                "live": True,
                "ready": self.ready,
                "draining": self.draining,
                "accepting_work": self.accepting_work,
                "readiness_error": self.readiness_error or None,
                "started_at": self.started_at,
                "uptime_seconds": round(max(0.0, time.time() - self.started_at), 3),
                "last_activity_at": self.last_activity_at,
            }


_states: dict[str, RuntimeHealth] = {}
_states_lock = threading.Lock()


def state_for(role: str) -> RuntimeHealth:
    with _states_lock:
        if role not in _states:
            _states[role] = RuntimeHealth(role=role)
        return _states[role]


def database_detail() -> Dict[str, Any]:
    """Bounded read-only database probe used only by detail/readiness."""
    from api.database import engine

    started = time.monotonic()
    try:
        with engine.connect() as connection:
            connection.exec_driver_sql("SELECT 1").scalar_one()
        return {"ok": True, "latency_ms": round((time.monotonic() - started) * 1000, 2)}
    except Exception as exc:
        return {
            "ok": False,
            "latency_ms": round((time.monotonic() - started) * 1000, 2),
            "error": type(exc).__name__,
        }


def build_health_router(
    role: str,
    *,
    detail_provider: Optional[DetailProvider] = None,
    include_compatibility_route: bool = True,
) -> APIRouter:
    state = state_for(role)
    router = APIRouter()

    def detail_payload() -> Dict[str, Any]:
        payload = state.snapshot()
        payload["database"] = database_detail()
        if detail_provider:
            try:
                payload.update(detail_provider())
            except Exception as exc:
                payload["detail_error"] = type(exc).__name__
        payload["ok"] = bool(payload["ready"] and payload["database"]["ok"])
        return payload

    @router.get("/health/live")
    def live() -> Dict[str, Any]:
        return {"ok": True, **state.snapshot()}

    @router.get("/health/ready")
    def ready() -> Dict[str, Any]:
        payload = detail_payload()
        if not payload["ok"]:
            raise HTTPException(status_code=503, detail=payload)
        return payload

    @router.get("/health/detail")
    def detail() -> Dict[str, Any]:
        return detail_payload()

    if include_compatibility_route:
        router.add_api_route("/health", detail, methods=["GET"], include_in_schema=False)
    return router
