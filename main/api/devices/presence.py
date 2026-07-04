"""Read/write helpers for the endpoint-agent presence snapshot.

Written by the process that owns the agent sockets (api-gateway, on
register / disconnect / bind) and read by every process during endpoint tool
discovery and classification. See ``api.models.device_presence``.
"""

import json
import time
from typing import Dict, List, Optional, Set, Tuple

from sqlmodel import Session, select

from ..database import engine
from ..models import DevicePresence

NON_MCP_CAPABILITIES: Set[str] = {"remote_control", "remote.control"}


def _int(value) -> Optional[int]:
    try:
        if value in (None, "", 0, "0"):
            return None
        return int(value)
    except (TypeError, ValueError):
        return None


def _decode(row: DevicePresence) -> Set[str]:
    try:
        parsed = json.loads(row.capabilities_json or "[]")
    except Exception:
        return set()
    if not isinstance(parsed, list):
        return set()
    return {str(x).strip() for x in parsed if str(x).strip()}


def mcp_capabilities(caps: Set[str]) -> Set[str]:
    """Endpoint capabilities that are real MCP tools.

    Some device capabilities, such as live remote-control support, are transport
    features rather than callable MCP tools and must not appear in prompt/tool
    permission surfaces.
    """
    return {name for name in caps if name not in NON_MCP_CAPABILITIES}


def _decode_defs(row: DevicePresence) -> Dict[str, dict]:
    try:
        parsed = json.loads(getattr(row, "tool_defs_json", "") or "{}")
    except Exception:
        return {}
    if not isinstance(parsed, dict):
        return {}
    out: Dict[str, dict] = {}
    for name, spec in parsed.items():
        key = str(name or "").strip()
        if not key or key in NON_MCP_CAPABILITIES or not isinstance(spec, dict):
            continue
        schema = spec.get("input_schema")
        out[key] = {
            "description": str(spec.get("description") or "").strip(),
            "input_schema": schema if isinstance(schema, dict) else {},
            "destructive": bool(spec.get("destructive")),
            "implementation": spec.get("implementation") if isinstance(spec.get("implementation"), dict) else {},
        }
    return out


def _load_presence_rows(session: Session, device_id: str):
    return session.exec(
        select(DevicePresence)
        .where(DevicePresence.device_id == device_id)
        .order_by(DevicePresence.updated_at.desc(), DevicePresence.id.desc())
    ).all()


def upsert_presence(
    user_id, device_id, ai_config_id, device_type, capabilities, online: bool = True, tool_defs=None,
    name=None, platform=None,
) -> None:
    aid = str(device_id or "").strip()
    if not aid:
        return
    caps = sorted({str(c).strip() for c in (capabilities or []) if str(c).strip()})
    defs = tool_defs if isinstance(tool_defs, dict) else {}
    uid = _int(user_id)
    with Session(engine) as session:
        rows = _load_presence_rows(session, aid)
        row = rows[0] if rows else None
        for stale in rows[1:]:
            session.delete(stale)
        if not row:
            row = DevicePresence(device_id=aid)
            session.add(row)
        row.user_id = uid or row.user_id or 0
        row.ai_config_id = _int(ai_config_id)
        row.device_type = str(device_type or "").strip()
        row.capabilities_json = json.dumps(caps, ensure_ascii=False)
        row.tool_defs_json = json.dumps(defs, ensure_ascii=False)
        row.online = bool(online)
        if name is not None:
            row.name = str(name or "").strip()
        if platform is not None:
            row.platform = str(platform or "").strip()
        row.updated_at = time.time()
        session.commit()


def set_offline(device_id) -> None:
    aid = str(device_id or "").strip()
    if not aid:
        return
    with Session(engine) as session:
        rows = _load_presence_rows(session, aid)
        row = rows[0] if rows else None
        dirty = bool(rows[1:])
        for stale in rows[1:]:
            session.delete(stale)
        if row and row.online:
            row.online = False
            row.updated_at = time.time()
            dirty = True
        if dirty:
            session.commit()


def update_binding(device_id, ai_config_id) -> None:
    aid = str(device_id or "").strip()
    if not aid:
        return
    with Session(engine) as session:
        rows = _load_presence_rows(session, aid)
        row = rows[0] if rows else None
        dirty = bool(rows[1:])
        for stale in rows[1:]:
            session.delete(stale)
        if row:
            row.ai_config_id = _int(ai_config_id)
            row.updated_at = time.time()
            dirty = True
        if dirty:
            session.commit()


def mark_all_offline() -> None:
    """Reset presence on a fresh gateway boot — sockets re-register and flip
    their own rows back online."""
    with Session(engine) as session:
        rows = session.exec(
            select(DevicePresence).where(DevicePresence.online == True)  # noqa: E712
        ).all()
        for row in rows:
            row.online = False
            row.updated_at = time.time()
        if rows:
            session.commit()


def online_devices_for_config(user_id, ai_config_id) -> List[Tuple[str, str, Set[str]]]:
    """``(device_id, device_type, mcp_capabilities)`` for every online agent bound
    to a config. ``device_id`` lets callers apply per-agent MCP scope."""
    cfg = _int(ai_config_id)
    if not cfg:
        return []
    uid = _int(user_id)
    out: List[Tuple[str, str, Set[str]]] = []
    with Session(engine) as session:
        rows = session.exec(
            select(DevicePresence).where(
                DevicePresence.ai_config_id == cfg,
                DevicePresence.online == True,  # noqa: E712
            ).order_by(DevicePresence.updated_at.desc(), DevicePresence.id.desc())
        ).all()
        seen_agents: Set[str] = set()
        for row in rows:
            device_id = str(row.device_id or "").strip()
            if not device_id or device_id in seen_agents:
                continue
            seen_agents.add(device_id)
            if uid and row.user_id and row.user_id != uid:
                continue
            out.append((device_id, str(row.device_type or "").strip(), mcp_capabilities(_decode(row))))
    return out


def online_tool_names() -> Tuple[Set[str], Set[str]]:
    """``(desktop_tools, browser_tools)`` advertised by online endpoint agents."""
    desktop: Set[str] = set()
    browser: Set[str] = set()
    with Session(engine) as session:
        rows = session.exec(
            select(DevicePresence)
            .where(DevicePresence.online == True)  # noqa: E712
            .order_by(DevicePresence.updated_at.desc(), DevicePresence.id.desc())
        ).all()
        seen_agents: Set[str] = set()
        for row in rows:
            device_id = str(row.device_id or "").strip()
            if not device_id or device_id in seen_agents:
                continue
            seen_agents.add(device_id)
            caps = mcp_capabilities(_decode(row))
            device_type = str(row.device_type or "").strip()
            if device_type == "workshop":
                continue
            if device_type == "browser":
                browser |= caps
            else:
                desktop |= caps
    return desktop, browser


def online_workshop_agents_for_user(user_id) -> List[Tuple[str, Set[str]]]:
    uid = _int(user_id)
    out: List[Tuple[str, Set[str]]] = []
    with Session(engine) as session:
        rows = session.exec(
            select(DevicePresence).where(
                DevicePresence.device_type == "workshop",
                DevicePresence.online == True,  # noqa: E712
            ).order_by(DevicePresence.updated_at.desc(), DevicePresence.id.desc())
        ).all()
        seen_agents: Set[str] = set()
        for row in rows:
            device_id = str(row.device_id or "").strip()
            if not device_id or device_id in seen_agents:
                continue
            seen_agents.add(device_id)
            if uid and row.user_id and row.user_id != uid:
                continue
            out.append((device_id, _decode(row)))
    return out


def online_tool_defs() -> Dict[str, dict]:
    """Merged ``{tool_name: {description, input_schema}}`` self-described by all
    online agents. The agent is the source of truth for its own tool schemas;
    the server reads them here instead of hardcoding per-tool schemas."""
    out: Dict[str, dict] = {}
    with Session(engine) as session:
        rows = session.exec(
            select(DevicePresence)
            .where(DevicePresence.online == True)  # noqa: E712
            .order_by(DevicePresence.updated_at.desc(), DevicePresence.id.desc())
        ).all()
        seen_agents: Set[str] = set()
        for row in rows:
            device_id = str(row.device_id or "").strip()
            if not device_id or device_id in seen_agents:
                continue
            seen_agents.add(device_id)
            for name, spec in _decode_defs(row).items():
                out.setdefault(name, {
                    **spec,
                    "mcpSource": str(row.device_type or "desktop").strip() or "desktop",
                })
    return out


def online_tool_defs_for_user(user_id) -> Dict[str, dict]:
    """Merged online endpoint definitions for one account only."""
    uid = _int(user_id)
    if uid is None:
        return {}
    out: Dict[str, dict] = {}
    with Session(engine) as session:
        rows = session.exec(
            select(DevicePresence).where(
                DevicePresence.user_id == uid,
                DevicePresence.online == True,  # noqa: E712
            ).order_by(DevicePresence.updated_at.desc(), DevicePresence.id.desc())
        ).all()
        seen_agents: Set[str] = set()
        for row in rows:
            device_id = str(row.device_id or "").strip()
            if not device_id or device_id in seen_agents:
                continue
            seen_agents.add(device_id)
            if str(row.device_type or "").strip() == "workshop":
                continue
            for name, spec in _decode_defs(row).items():
                out.setdefault(name, {
                    **spec,
                    "mcpSource": str(row.device_type or "desktop").strip() or "desktop",
                    "device_id": device_id,
                })
    return out


def online_tool_catalog_for_user(user_id) -> List[Dict[str, object]]:
    """Online endpoint MCP definitions owned by one user, grouped by device.

    Unlike ``online_tool_defs`` this helper is safe for user-facing knowledge
    views: it never merges another account's endpoint metadata into the result.
    """
    uid = _int(user_id)
    if uid is None:
        return []
    out: List[Dict[str, object]] = []
    with Session(engine) as session:
        rows = session.exec(
            select(DevicePresence).where(
                DevicePresence.user_id == uid,
                DevicePresence.online == True,  # noqa: E712
            ).order_by(DevicePresence.updated_at.desc(), DevicePresence.id.desc())
        ).all()
        seen_agents: Set[str] = set()
        for row in rows:
            device_id = str(row.device_id or "").strip()
            if not device_id or device_id in seen_agents:
                continue
            seen_agents.add(device_id)
            device_type = str(row.device_type or "desktop").strip() or "desktop"
            if device_type == "workshop":
                continue
            capabilities = mcp_capabilities(_decode(row))
            defs = _decode_defs(row)
            tools = []
            for name in sorted(capabilities):
                spec = defs.get(name, {})
                tools.append({
                    "name": name,
                    "description": str(spec.get("description") or "").strip(),
                    "input_schema": spec.get("input_schema") if isinstance(spec.get("input_schema"), dict) else {},
                    "destructive": bool(spec.get("destructive")),
                    "implementation": spec.get("implementation") if isinstance(spec.get("implementation"), dict) else {},
                })
            out.append({
                "device_id": device_id,
                "device_type": device_type,
                "updated_at": float(row.updated_at or 0),
                "tools": tools,
            })
    return out


def offline_devices_for_user(user_id, exclude_device_ids: Set[str]) -> List[dict]:
    """Endpoint-agent rows the Workshop panel should still list while offline.

    One row per ``device_id`` this user has ever registered (last-known name /
    platform / capabilities), skipping ids already covered by the live socket
    snapshot. Lets an operator save an AI assignment for a device that isn't
    currently connected; the binding takes effect on its next register."""
    uid = _int(user_id)
    if uid is None:
        return []
    exclude = {str(x).strip() for x in (exclude_device_ids or set()) if str(x).strip()}
    out: List[dict] = []
    with Session(engine) as session:
        rows = session.exec(
            select(DevicePresence)
            .where(DevicePresence.user_id == uid)
            .order_by(DevicePresence.updated_at.desc(), DevicePresence.id.desc())
        ).all()
        seen: Set[str] = set()
        for row in rows:
            device_id = str(row.device_id or "").strip()
            if not device_id or device_id in seen or device_id in exclude:
                continue
            seen.add(device_id)
            device_type = str(row.device_type or "").strip()
            if device_type in ("workshop", "toolbox"):
                continue  # built-ins are synthesized live, never persisted offline rows
            out.append({
                "id": device_id,
                "name": str(row.name or "").strip() or device_id,
                "platform": str(row.platform or "").strip(),
                "aiConfigId": row.ai_config_id,
                "isWindowsDesktop": device_type == "desktop",
                "isBrowserExtension": device_type == "browser",
                "isAndroid": device_type == "android",
                "capabilities": sorted(mcp_capabilities(_decode(row))),
                "version": "",
                "lifecycle": "offline",
                "online": False,
                "connectedAt": None,
                "lastTaskId": None,
                "lastTaskStatus": None,
                "lastTaskAt": None,
                "lastError": None,
            })
    return out


def tool_defs_for_agent(user_id, device_id) -> Dict[str, dict]:
    """Self-described tool definitions for one user-owned endpoint agent."""
    uid = _int(user_id)
    aid = str(device_id or "").strip()
    if uid is None or not aid:
        return {}
    with Session(engine) as session:
        rows = _load_presence_rows(session, aid)
        for row in rows:
            if row.user_id and row.user_id != uid:
                continue
            return _decode_defs(row)
    return {}
