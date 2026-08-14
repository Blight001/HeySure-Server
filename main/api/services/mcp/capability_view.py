"""Resolve one AI's authoritative MCP capability surface.

The resolver is shared by Gateway, AI Runtime and MCP Runtime.  It has no
import-time side effects; all runtime registries and database-backed adapters
are loaded only when a view is explicitly requested.
"""

from __future__ import annotations

from dataclasses import dataclass
from types import MappingProxyType
from typing import Any, Iterable, Mapping, Optional

from fastapi import HTTPException
from sqlmodel import Session, select

from api.models import AssistantAIConfig, DevicePresence, User
from api.services.mcp.capability_revision import capability_revision, schema_revision
from api.services.mcp.capability_types import (
    DevicePromptMetadata,
    ScopedToolView,
    ToolBlock,
    ToolCapability,
)


@dataclass(frozen=True)
class ToolViewRequest:
    ai_config_id: Optional[int]
    selected_tools: Optional[frozenset[str]] = None
    task_required_tools: frozenset[str] = frozenset()
    override_tools: Optional[frozenset[str]] = None
    extra_required_tools: frozenset[str] = frozenset()


def resolve_scoped_tool_view(
    session: Session,
    user: User,
    cfg: Optional[AssistantAIConfig],
    request: ToolViewRequest,
) -> ScopedToolView:
    """Build the process-independent eligible tool view for one AI."""
    user_id = int(user.id or 0)
    ai_config_id = request.ai_config_id
    catalog, schema_conflicts = _capability_catalog(user_id, ai_config_id)
    registry_names = {name for name, item in catalog.items() if item.source_kind == "server"}
    eligible_names, endpoint_names = _resolve_eligible_names(
        user, cfg, request, registry_names
    )
    eligible_names -= schema_conflicts
    eligible = {
        name: catalog.get(name, _placeholder_capability(name, endpoint_names))
        for name in sorted(eligible_names)
        if name in catalog or name in endpoint_names
    }
    blocked = {
        name: ToolBlock(name=name, reason="not_eligible")
        for name in sorted(set(catalog) - set(eligible))
    }
    blocked.update({
        name: ToolBlock(name=name, reason="schema_conflict")
        for name in sorted(schema_conflicts)
    })
    devices, device_tool_names = _device_projection(
        session, user_id, ai_config_id, set(eligible)
    )
    revision = capability_revision(
        eligible,
        devices,
        selected_tools=request.selected_tools,
    )
    return ScopedToolView(
        revision=revision,
        eligible=MappingProxyType(eligible),
        blocked=MappingProxyType(blocked),
        devices=tuple(devices),
        device_tool_names=MappingProxyType(device_tool_names),
    )


def _resolve_eligible_names(user, cfg, request, registry_names):
    from connector_runtime.dispatch.desktop_device_tools import (
        endpoint_bridge_tools_for_config,
        endpoint_tools_for_config,
        toolbox_tools_for_config,
    )
    from mcp_runtime.mcp.core import MCP_INTROSPECTION_TOOLS
    from mcp_runtime.mcp.permissions import LIBRARY_BOUND_TOOLS
    from api.services.mcp.mcp_tool_aliases import fully_clean_tool_names

    user_id = int(user.id or 0)
    ai_config_id = request.ai_config_id
    introspection = set(MCP_INTROSPECTION_TOOLS)
    if cfg is not None and not bool(getattr(cfg, "mcp_enabled", False)):
        return introspection, set()
    endpoint_names = set(endpoint_tools_for_config(ai_config_id, user_id))
    bridge_names = set(endpoint_bridge_tools_for_config(ai_config_id, user_id))
    toolbox_names = _safe_toolbox_names(toolbox_tools_for_config, ai_config_id, user_id)
    library_names = _library_tools_for_config(
        user_id,
        ai_config_id,
        registry_names & set(LIBRARY_BOUND_TOOLS),
    )
    eligible_names = endpoint_names | bridge_names | toolbox_names | library_names | introspection
    eligible_names = _apply_task_override(eligible_names, request.override_tools, introspection)
    eligible_names = fully_clean_tool_names(eligible_names)
    eligible_names = _apply_selected_scope(
        eligible_names,
        request.selected_tools,
        introspection,
    )
    return eligible_names, endpoint_names


def scoped_tool_view_for_ids(
    user_id: int,
    ai_config_id: Optional[int],
    *,
    selected_tools: Optional[Iterable[str]] = None,
    task_required_tools: Iterable[str] = (),
    override_tools: Optional[Iterable[str]] = None,
    extra_required_tools: Iterable[str] = (),
) -> ScopedToolView:
    """Resolve a view from stable ids for describe and execution guards."""
    from api.database import engine

    with Session(engine) as session:
        user = session.get(User, int(user_id))
        if user is None:
            raise HTTPException(status_code=404, detail="User not found")
        cfg = _config_for(session, int(user_id), ai_config_id)
        return resolve_scoped_tool_view(
            session,
            user,
            cfg,
            ToolViewRequest(
                ai_config_id=ai_config_id,
                selected_tools=_optional_frozenset(selected_tools),
                task_required_tools=frozenset(task_required_tools),
                override_tools=_optional_frozenset(override_tools),
                extra_required_tools=frozenset(extra_required_tools),
            ),
        )


def ensure_tool_eligible(user_id: int, ai_config_id: Optional[int], tool_name: str) -> ScopedToolView:
    """Re-resolve eligibility at call time and reject stale/revoked access."""
    if ai_config_id is None:
        return scoped_tool_view_for_ids(user_id, None)
    view = scoped_tool_view_for_ids(user_id, ai_config_id)
    if str(tool_name or "").strip() not in view.eligible:
        raise HTTPException(status_code=403, detail="Tool is not available for this AI")
    return view


def _capability_catalog(
    user_id: int,
    ai_config_id: Optional[int],
) -> tuple[dict[str, ToolCapability], set[str]]:
    from mcp_runtime.mcp import registry

    out: dict[str, ToolCapability] = {}
    conflicts: set[str] = set()
    for item in registry.list_tools():
        capability = _server_capability(item, user_id)
        if capability.canonical_name:
            out[capability.canonical_name] = capability
    for capability in _bound_device_capabilities(user_id, ai_config_id):
        _merge_device_capability(out, conflicts, capability)
    try:
        from library import engine as workshop_engine

        for name, item in workshop_engine.tool_defs_map().items():
            if name not in out:
                out[name] = _device_capability(item, "workshop", "workshop", name=name)
    except Exception:
        pass
    return out, conflicts


def _bound_device_capabilities(user_id: int, ai_config_id: Optional[int]):
    from api.devices.bindings import device_ids_for_config
    from api.devices.presence import online_tool_catalog_for_user

    if ai_config_id is None:
        return
    bound_ids = set(device_ids_for_config(user_id, ai_config_id))
    for device in online_tool_catalog_for_user(user_id):
        device_id = str(device.get("device_id") or "").strip()
        if device_id not in bound_ids:
            continue
        device_type = str(device.get("device_type") or "desktop").strip() or "desktop"
        for item in device.get("tools") or []:
            yield _device_capability(item, device_id, device_type)


def _merge_device_capability(out, conflicts, capability) -> None:
    name = capability.canonical_name
    if not name:
        return
    previous = out.get(name)
    if previous is None:
        out[name] = capability
        return
    if (
        previous.source_kind != capability.source_kind
        or previous.schema_version != capability.schema_version
    ):
        # A model-visible name can have only one execution contract. Different
        # schemas (or a server/device collision) fail closed until providers agree.
        conflicts.add(name)


def _server_capability(item: Mapping[str, Any], user_id: int) -> ToolCapability:
    name = str(item.get("name") or "").strip()
    description = str(item.get("description") or "").strip()
    schema = item.get("inputSchema") if isinstance(item.get("inputSchema"), dict) else {}
    destructive = bool(item.get("destructive"))
    if user_id:
        try:
            from api.services.knowledge.librarian_service import (
                intrinsic_input_schema,
                intrinsic_tool_description,
            )

            description = intrinsic_tool_description(user_id, name, description)
            schema = intrinsic_input_schema(user_id, name, schema)
        except Exception:
            pass
    return ToolCapability(
        canonical_name=name,
        description=description,
        input_schema=MappingProxyType(dict(schema)),
        schema_version=schema_revision(name, description, schema, destructive),
        source_kind="server",
        provider_id="server",
        destructive=destructive,
    )


def _device_capability(
    item: Mapping[str, Any], device_id: str, device_type: str, *, name: str = ""
) -> ToolCapability:
    canonical = str(name or item.get("name") or "").strip()
    description = str(item.get("description") or "").strip()
    schema = item.get("input_schema") if isinstance(item.get("input_schema"), dict) else {}
    if not schema and isinstance(item.get("inputSchema"), dict):
        schema = item.get("inputSchema")
    destructive = bool(item.get("destructive", True))
    implementation = item.get("implementation") if isinstance(item.get("implementation"), dict) else {}
    return ToolCapability(
        canonical_name=canonical,
        description=description,
        input_schema=MappingProxyType(dict(schema)),
        implementation=MappingProxyType(dict(implementation)),
        schema_version=schema_revision(
            canonical, description, schema, destructive, implementation
        ),
        source_kind="workshop" if device_type == "workshop" else "device",
        provider_id=device_id,
        device_id=device_id,
        destructive=destructive,
    )


def _placeholder_capability(name: str, endpoint_names: set[str]) -> ToolCapability:
    return ToolCapability(
        canonical_name=name,
        source_kind="device" if name in endpoint_names else "server",
        destructive=name in endpoint_names,
    )


def _apply_task_override(
    names: set[str],
    override: Optional[frozenset[str]],
    retained: set[str],
) -> set[str]:
    if override is None:
        return set(names)
    return (set(names) & set(override)) | (set(names) & set(retained))


def _library_tools_for_config(user_id, ai_config_id, library_tools):
    """Resolve built-in library tools from its device binding and member scope."""
    if not ai_config_id or not library_tools:
        return set()
    try:
        from api.devices.mcp_permissions import get_scope
        from api.devices.workshop_bindings import config_bound_to_library
        from library.engine import device_id_for_user

        if not config_bound_to_library(user_id, ai_config_id):
            return set()
        scope = get_scope(user_id, device_id_for_user(user_id), ai_config_id)
        return set(library_tools) if scope is None else set(library_tools) & set(scope)
    except Exception:
        return set()


def _apply_selected_scope(names, selected, preserved):
    if selected is None:
        return set(names)
    from api.services.mcp.mcp_tool_aliases import fully_clean_tool_names, resolve_tool_name

    selected_names = fully_clean_tool_names(selected)
    selected_names |= {resolve_tool_name(name, names) for name in tuple(selected_names)}
    selected_names |= set(preserved)
    return set(names) & selected_names


def _device_projection(session, user_id, ai_config_id, eligible_names):
    from api.devices.mcp_permissions import get_scope
    from api.devices.presence import _decode, mcp_capabilities
    from api.devices.bindings import device_ids_for_config

    if not ai_config_id:
        return [], {}
    bound_ids = set(device_ids_for_config(user_id, ai_config_id))
    if not bound_ids:
        return [], {}
    rows = session.exec(
        select(DevicePresence).where(
            DevicePresence.user_id == user_id,
            DevicePresence.device_id.in_(bound_ids),
            DevicePresence.online == True,  # noqa: E712
        ).order_by(DevicePresence.device_id.asc(), DevicePresence.updated_at.desc())
    ).all()
    devices = []
    by_device = {}
    seen = set()
    for row in rows:
        device_id = str(row.device_id or "").strip()
        if not device_id or device_id in seen:
            continue
        seen.add(device_id)
        scope = get_scope(user_id, device_id, ai_config_id) or set()
        tool_names = frozenset(mcp_capabilities(_decode(row)) & scope & eligible_names)
        by_device[device_id] = tool_names
        devices.append(_device_metadata(row, len(tool_names)))
    return devices, by_device


def _device_metadata(row: DevicePresence, tool_count: int) -> DevicePromptMetadata:
    try:
        from api.devices.presence import device_prompt_metadata

        item = device_prompt_metadata(row, tool_count=tool_count)
    except (ImportError, AttributeError):
        item = {}
    return DevicePromptMetadata(
        device_id=str(item.get("device_id") or row.device_id or "").strip(),
        name=str(item.get("name") or row.name or "").strip(),
        device_type=str(item.get("device_type") or row.device_type or "").strip(),
        purpose=str(item.get("purpose") or "").strip(),
        tool_count=int(item.get("tool_count") or tool_count),
        catalog_generation=int(item.get("catalog_generation") or getattr(row, "catalog_generation", 0) or 0),
        catalog_hash=str(item.get("catalog_hash") or getattr(row, "catalog_hash", "") or "").strip(),
    )


def _config_for(session, user_id, ai_config_id):
    if ai_config_id is None:
        return None
    cfg = session.exec(select(AssistantAIConfig).where(
        AssistantAIConfig.user_id == user_id,
        AssistantAIConfig.id == int(ai_config_id),
    )).first()
    if cfg is None:
        raise HTTPException(status_code=404, detail="AI config not found")
    return cfg


def _safe_toolbox_names(resolver, ai_config_id, user_id):
    try:
        return set(resolver(ai_config_id, user_id))
    except Exception:
        return set()


def _optional_frozenset(values):
    return None if values is None else frozenset(str(value) for value in values if str(value))
