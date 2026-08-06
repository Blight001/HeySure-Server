"""Read/write helpers for per-agent endpoint MCP permission scope.

Kept separate from the socket / REST layers so the dispatch path (which reads
the scope on every endpoint tool call) and the Workshop / AI-settings editors
(which write it) share one source of truth. See
``api.models.device_mcp_permission``.

Scope is keyed by ``(user_id, device_id)`` — each individual connected agent has
its own allow-list.

A missing row means the agent has never had a scope initialized (treated as
closed / no tools at runtime dispatch). On first connect, ``reconcile_scope_with_capabilities`` initializes the scope
to the full live capability set. On later reconnects it preserves the user's
saved allow-list and only removes capabilities that no longer exist.

``get_scope`` returns ``None`` only for "no record ever"; a row with ``[]``
means explicitly none allowed. User saves therefore survive refreshes and
reconnects, including an explicit "全不选" selection.
"""

import json
import time
from typing import Iterable, Optional, Set

from sqlmodel import Session, select

from ..database import engine
from ..models import DeviceTypeMcpPermission

VALID_AGENT_TYPES = ("linux", "desktop", "browser", "android", "workshop", "toolbox", "custom")


def _coerce_int(value) -> Optional[int]:
    try:
        if value in (None, "", 0, "0"):
            return None
        return int(value)
    except (TypeError, ValueError):
        return None


def _device_id(value) -> str:
    return str(value or "").strip()


def _normalize_type(device_type) -> str:
    value = str(device_type or "").strip().lower()
    return value if value in VALID_AGENT_TYPES else ""


def _decode_tools(raw: str) -> Set[str]:
    try:
        parsed = json.loads(raw or "[]")
    except Exception:
        return set()
    if not isinstance(parsed, list):
        return set()
    return {str(item).strip() for item in parsed if isinstance(item, str) and str(item).strip()}


def _load_scope_rows(session: Session, user_id: int, device_id: str):
    return session.exec(
        select(DeviceTypeMcpPermission)
        .where(
            DeviceTypeMcpPermission.user_id == user_id,
            DeviceTypeMcpPermission.device_id == device_id,
        )
        .order_by(DeviceTypeMcpPermission.updated_at.desc(), DeviceTypeMcpPermission.id.desc())
    ).all()


def get_scope(user_id, device_id) -> Optional[Set[str]]:
    """Return the saved allow-list for (user, agent), or ``None`` when no row
    exists (never initialized for this agent). For newly connected devices the
    row is auto-created with the full capability set on first register."""
    uid = _coerce_int(user_id)
    aid = _device_id(device_id)
    if uid is None or not aid:
        return None
    with Session(engine) as session:
        rows = _load_scope_rows(session, uid, aid)
        row = rows[0] if rows else None
        for stale in rows[1:]:
            session.delete(stale)
        if rows[1:]:
            session.commit()
        return _decode_tools(row.tools_json) if row else None


def set_scope(user_id, device_id, tools: Iterable[str], *, ai_config_id=None, device_type="") -> Optional[Set[str]]:
    """Upsert the allow-list for one agent. ``ai_config_id`` / ``device_type`` are
    stored as informational columns. Returns the stored set, or ``None`` on bad
    input."""
    uid = _coerce_int(user_id)
    aid = _device_id(device_id)
    if uid is None or not aid:
        return None
    allowed = sorted({str(item).strip() for item in (tools or []) if str(item).strip()})
    encoded = json.dumps(allowed, ensure_ascii=False)
    cfg = _coerce_int(ai_config_id)
    atype = _normalize_type(device_type)
    with Session(engine) as session:
        rows = _load_scope_rows(session, uid, aid)
        row = rows[0] if rows else None
        for stale in rows[1:]:
            session.delete(stale)
        if row:
            row.tools_json = encoded
            row.ai_config_id = cfg
            row.device_type = atype or row.device_type
            row.updated_at = time.time()
        else:
            row = DeviceTypeMcpPermission(
                user_id=uid, device_id=aid, ai_config_id=cfg, device_type=atype, tools_json=encoded
            )
            session.add(row)
        session.commit()
    return set(allowed)


def delete_scope(user_id, device_id) -> None:
    """Drop the saved allow-list for (user, agent), e.g. when forgetting an
    offline device's record entirely."""
    uid = _coerce_int(user_id)
    aid = _device_id(device_id)
    if uid is None or not aid:
        return
    with Session(engine) as session:
        rows = _load_scope_rows(session, uid, aid)
        for row in rows:
            session.delete(row)
        if rows:
            session.commit()


def reconcile_scope_with_capabilities(
    user_id,
    device_id,
    capabilities: Iterable[str],
    *,
    ai_config_id=None,
    device_type="",
) -> Optional[Set[str]]:
    """Reconcile a saved scope with the agent's current capabilities.

    A first-time device defaults to all reported tools. Existing rows preserve
    the user's explicit selection (including an empty set) and only prune tools
    the device no longer reports. This function is called on reconnect and after
    dynamic tool pushes, so it must never widen an already-saved scope.
    """
    uid = _coerce_int(user_id)
    aid = _device_id(device_id)
    if uid is None or not aid:
        return None
    live_caps = {str(item).strip() for item in (capabilities or []) if str(item).strip()}
    cfg = _coerce_int(ai_config_id)
    atype = _normalize_type(device_type)
    with Session(engine) as session:
        rows = _load_scope_rows(session, uid, aid)
        row = rows[0] if rows else None
        dirty = bool(rows[1:])
        for stale in rows[1:]:
            session.delete(stale)
        if not row:
            if dirty:
                session.commit()
            if live_caps:
                # (Re)connect / first time: default to full set so the
                # Workshop MCP permission editor + runtime grants see everything.
                return set_scope(uid, aid, live_caps, ai_config_id=cfg, device_type=atype)
            return None

        # Preserve the operator's saved subset, including an explicit empty
        # selection. Reconnect/refresh may prune stale tools but must not grant
        # tools that the operator did not select.
        reconciled = sorted(_decode_tools(row.tools_json) & live_caps)
        if set(reconciled) != _decode_tools(row.tools_json):
            row.tools_json = json.dumps(reconciled, ensure_ascii=False)
            row.updated_at = time.time()
            dirty = True
        if cfg is not None and row.ai_config_id != cfg:
            row.ai_config_id = cfg
            dirty = True
        if atype and row.device_type != atype:
            row.device_type = atype
            dirty = True
        if dirty:
            session.commit()
        return set(reconciled)
