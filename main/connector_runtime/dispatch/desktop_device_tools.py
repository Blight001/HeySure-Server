import time
from typing import Any, Dict, List, Optional, Set, Tuple

from api.sio import agents

# The live ``agents`` registry only exists in the process that owns the agent
# socket server (api-gateway). To let every process (ai-runtime / mcp-runtime /
# connector) discover and classify endpoint tools identically, connected agents
# are mirrored into a shared DB presence snapshot (see ``api.devices.presence``).
# Classification (``is_desktop_tool`` / ``is_browser_tool``) consults a short
# TTL cache of that snapshot so it stays context-free and cheap across
# processes.
_TOOLNAME_CACHE: Dict[str, Any] = {"expiry": 0.0, "desktop": set(), "browser": set()}
_TOOLDEFS_CACHE: Dict[str, Any] = {"expiry": 0.0, "defs": {}}
_TOOLNAME_TTL_SECONDS = 3.0


def _presence_tool_names() -> Tuple[Set[str], Set[str]]:
    now = time.time()
    if _TOOLNAME_CACHE["expiry"] > now:
        return _TOOLNAME_CACHE["desktop"], _TOOLNAME_CACHE["browser"]
    try:
        from api.devices.presence import online_tool_names
        desktop, browser = online_tool_names()
    except Exception:
        desktop, browser = set(), set()
    _TOOLNAME_CACHE.update(expiry=now + _TOOLNAME_TTL_SECONDS, desktop=desktop, browser=browser)
    return desktop, browser


def _presence_tool_defs() -> Dict[str, Dict[str, Any]]:
    """Short-TTL cache of every online agent's self-described tool schemas.
    The agent owns its schemas; the server reads them here so it never
    hardcodes per-tool descriptions / input schemas."""
    now = time.time()
    if _TOOLDEFS_CACHE["expiry"] > now:
        return _TOOLDEFS_CACHE["defs"]
    try:
        from api.devices.presence import online_tool_defs
        defs = online_tool_defs()
    except Exception:
        defs = {}
    _TOOLDEFS_CACHE.update(expiry=now + _TOOLNAME_TTL_SECONDS, defs=defs)
    return defs



# No server tool is exposed merely because an endpoint is connected. Member and
# device inventory now comes from the library-bound ``member.manage`` tool.
ENDPOINT_BRIDGE_MCP_TOOLS: Set[str] = set()

# The endpoint (desktop / browser) tool surface is no longer a hardcoded
# whitelist. Each connected agent advertises its own tools in the
# ``capabilities`` array of ``device:register`` (see ``api/socket_events.py``),
# and the server derives everything below from that live list. A tool a
# Windows agent gains at runtime (``speech.*``, ``vision.*``, ``hands.*`` …)
# therefore becomes dispatchable with no server redeploy. Browser tools are
# recognised by their ``browser_`` / ``card_`` namespace; everything else a
# desktop agent reports is a desktop tool.

ENDPOINT_TOOL_PREFIXES = (
    "browser_",
    "card_",
    "desktop_",
    "run_command",
    "clipboard",
    "fs.",
    "shell.",
    "git.",
    "keyboard.",
    "mouse.",
    "screen.",
    "ui.",
    "window.",
    "process.",
    "display.",
    "ear.",
    "hands.",
    # 图书馆内置设备（兼容 agent/workshop/）：这两个域已从内置 MCP 迁出，运行时
    # 可用性只由"AI ↔ 设备绑定 + per-agent scope"决定，持久化配置里的残留
    # 条目一律剥离，避免绕过绑定门槛。
    "librarian.",
    "evolution.",
)


def _is_browser_namespaced(name: str) -> bool:
    tool = str(name or "").strip()
    return tool.startswith("browser_") or tool.startswith("card_")


def is_endpoint_tool_config_name(name: str) -> bool:
    """Static guard for endpoint tools accidentally stored in AI ``mcp_tools``.

    Runtime availability still comes from live agent capabilities + per-agent
    scope. This prefix test only strips legacy endpoint entries from persisted
    AI config / task override allow-lists, where dynamic presence lookups are
    the wrong source of truth.
    """
    tool = str(name or "").strip()
    return bool(tool) and tool.startswith(ENDPOINT_TOOL_PREFIXES)


def strip_endpoint_tool_config_names(names: Set[str]) -> Set[str]:
    return {name for name in names if not is_endpoint_tool_config_name(name)}


# 兼容设备域（agent/workshop/）注册的工具走 evolution. 命名空间。知识库操作已
# 统一为注册表工具 knowledge.manage，不再经内置设备分发。
WORKSHOP_TOOL_PREFIXES = ("evolution.",)


def is_workshop_tool(name: str) -> bool:
    tool = str(name or "").strip()
    return bool(tool) and tool.startswith(WORKSHOP_TOOL_PREFIXES)


# Device types the server understands natively. Developer-built devices that
# follow device/read.md register with ``deviceType: "custom"`` (any other
# unrecognised declared type also lands on "custom") and are routed through
# the desktop-class dispatch channel like android.
KNOWN_DEVICE_TYPES = {"desktop", "browser", "android", "workshop", "toolbox", "custom"}


def device_type_of(agent: Optional[Dict[str, Any]]) -> Optional[str]:
    """Classify a connected-agent record as ``"desktop"`` / ``"browser"`` /
    ``"android"`` (手机端) / ``"workshop"`` (兼容类型：图书馆设备) / ``"toolbox"`` (内置工具箱)
    / ``"custom"`` (开发者自建设备，见 device/read.md).

    Android phones are a distinct type (so they are never seeded the desktop
    python/shell dynamic tools they cannot run, and get their own label /
    permission group), but for *dispatch routing* they are a desktop-class
    executor — their tap/swipe/screen tools flow through the desktop channel
    (see ``_reported_endpoint_tools`` / ``get_connected_desktop_agent``).
    Custom devices are desktop-class for routing too."""
    if not isinstance(agent, dict):
        return None
    if bool(agent.get("isToolbox")):
        return "toolbox"
    declared = str(agent.get("deviceType") or agent.get("device_type") or "").strip().lower()
    if declared in KNOWN_DEVICE_TYPES:
        return declared
    platform = str(agent.get("platform") or "").lower()
    if bool(agent.get("isWorkshop")) or "workshop" in platform:
        return "workshop"
    if bool(agent.get("isBrowserExtension")) or "browser-extension" in platform:
        return "browser"
    if bool(agent.get("isAndroid")) or "android" in platform:
        return "android"
    if bool(agent.get("isWindowsDesktop")) or "desktop" in platform or "windows" in platform:
        return "desktop"
    # A device that declares *some* type (or the explicit flag) but matches no
    # builtin form is a developer-built custom device — accept it instead of
    # dropping it from presence / tool discovery.
    if declared or bool(agent.get("isCustomDevice")):
        return "custom"
    return None


def _builtin_library_tools(agent: Dict[str, Any], device_type: str) -> Set[str]:
    """Governance tools accepted only from the server-owned library device."""
    if device_type != "workshop":
        return set()
    if str(agent.get("source") or "").strip().lower() != "builtin":
        return set()
    try:
        from library.engine import is_builtin_workshop_device_id
        from mcp_runtime.mcp.permissions import LIBRARY_BOUND_TOOLS

        if is_builtin_workshop_device_id(agent.get("id")):
            reported = {str(cap or "").strip() for cap in agent.get("capabilities") or []}
            return set(LIBRARY_BOUND_TOOLS) & reported
    except Exception:
        pass
    return set()


def _agent_capabilities(agent: Dict[str, Any], device_type: str) -> Set[str]:
    """Tool names that ``agent`` reports, owned by its actual device type.

    Browser extensions may create dynamically named MCP tools, so their
    capabilities cannot be restricted to the historical ``browser_*`` prefix.
    Workshop devices remain namespace-restricted because that channel has a
    separate trust and binding model.
    Toolbox builtin reports its server-fixed tools directly (no prefix filter).
    """
    names: Set[str] = set()
    if device_type == "toolbox":
        for cap in agent.get("capabilities") or []:
            name = str(cap or "").strip()
            if name:
                names.add(name)
        return names
    try:
        from api.devices.presence import NON_MCP_CAPABILITIES
    except Exception:
        NON_MCP_CAPABILITIES = {
            "remote_control", "remote.control",
            "remote_terminal", "remote.terminal",
        }
    for cap in agent.get("capabilities") or []:
        name = str(cap or "").strip()
        if not name or name in NON_MCP_CAPABILITIES:
            continue
        if device_type == "workshop":
            if is_workshop_tool(name):
                names.add(name)
        elif device_type == "browser":
            if not is_workshop_tool(name):
                names.add(name)
        else:
            if not _is_browser_namespaced(name) and not is_workshop_tool(name):
                names.add(name)
    return names


def agent_endpoint_tools(agent: Optional[Dict[str, Any]]) -> Set[str]:
    """Endpoint tool names a single connected agent reports, classified by its
    own type. Used by the per-agent permission editor."""
    atype = device_type_of(agent)
    if not atype or not isinstance(agent, dict):
        return set()
    return _agent_capabilities(agent, atype) | _builtin_library_tools(agent, atype)


def agent_endpoint_tool_defs(agent: Optional[Dict[str, Any]]) -> Dict[str, Dict[str, Any]]:
    """``{name: {description, input_schema}}`` self-described by a single agent
    (via the ``toolDefs`` it ships in ``device:register``), restricted to the
    tools of its own type. The agent is the source of truth for its own tool
    schemas, so the server stores these verbatim instead of hardcoding them.
    A tool reported without a def simply gets no entry (generic fallback)."""
    atype = device_type_of(agent)
    if not atype or not isinstance(agent, dict):
        return {}
    allowed = _agent_capabilities(agent, atype)
    out: Dict[str, Dict[str, Any]] = {}
    for raw in agent.get("toolDefs") or []:
        if not isinstance(raw, dict):
            continue
        name = str(raw.get("name") or "").strip()
        if not name or name not in allowed:
            continue
        schema = raw.get("input_schema")
        if not isinstance(schema, dict):
            schema = raw.get("inputSchema") if isinstance(raw.get("inputSchema"), dict) else {}
        out[name] = {
            "description": str(raw.get("description") or "").strip(),
            "input_schema": schema,
            "destructive": bool(raw.get("destructive")),
            "implementation": raw.get("implementation") if isinstance(raw.get("implementation"), dict) else {},
        }
    return out


def _reported_endpoint_tools(*, want_desktop: bool) -> Set[str]:
    """Every tool name advertised by currently-connected agents of one kind.

    ``want_desktop`` covers real desktops, Android phones and custom devices:
    all are desktop-class executors for dispatch, so their tools are routed
    through ``is_desktop_tool`` → ``get_connected_desktop_agent``."""
    targets = {"desktop", "android", "custom"} if want_desktop else {"browser"}
    names: Set[str] = set()
    for agent in list(agents.values()):
        atype = device_type_of(agent)
        if atype not in targets:
            continue
        names.update(_agent_capabilities(agent, atype))
    return names


def desktop_tool_names() -> Set[str]:
    """All desktop tool names currently advertised by connected desktop agents."""
    return _reported_endpoint_tools(want_desktop=True)


def browser_tool_names() -> Set[str]:
    """All browser tool names currently advertised by connected browser agents."""
    return _reported_endpoint_tools(want_desktop=False)


def is_desktop_tool(name: str) -> bool:
    tool = str(name or "").strip()
    if is_workshop_tool(tool):
        return False
    desktop_live = desktop_tool_names()
    browser_live = browser_tool_names()
    if tool in desktop_live:
        return True
    if tool in browser_live:
        return False
    return tool in _presence_tool_names()[0]


def is_browser_tool(name: str) -> bool:
    tool = str(name or "").strip()
    if _is_browser_namespaced(tool):
        return True
    browser_live = browser_tool_names()
    desktop_live = desktop_tool_names()
    if tool in browser_live and tool not in desktop_live:
        return True
    desktop_presence, browser_presence = _presence_tool_names()
    if tool in browser_presence and tool not in desktop_presence:
        return True
    return False


def is_endpoint_agent_tool(name: str) -> bool:
    return is_workshop_tool(name) or is_desktop_tool(name) or is_browser_tool(name)


def connected_endpoint_tool_catalog() -> List[Dict[str, str]]:
    """Live endpoint tool catalog: every tool an online desktop / browser agent
    currently advertises (from the shared presence snapshot), tagged by
    ``mcpSource``."""
    desktop, browser = _presence_tool_names()
    catalog: Dict[str, str] = {}
    for name in desktop:
        catalog[name] = "desktop"
    for name in browser:
        catalog.setdefault(name, "browser")
    return [
        {"name": name, "mcpSource": catalog[name]}
        for name in sorted(catalog)
    ]


# A tool reported by an agent that ships no schema (legacy clients) gets this
# permissive object schema so the model can still pass arbitrary arguments.
_GENERIC_ENDPOINT_SCHEMA: Dict[str, Any] = {
    "type": "object",
    "properties": {},
    "required": [],
    "additionalProperties": True,
}


def endpoint_tool_description(name: str) -> str:
    """Description for an endpoint tool.

    The agent is the source of truth. If it does not report a description, the
    backend returns an empty string instead of inventing one.
    """
    tool = str(name or "").strip()
    reported = _presence_tool_defs().get(tool)
    if reported and reported.get("description"):
        return str(reported["description"]).strip()
    return ""


def endpoint_tool_input_schema(name: str) -> Dict[str, Any]:
    """Input schema for an endpoint tool, taken verbatim from the agent."""
    tool = str(name or "").strip()
    reported = _presence_tool_defs().get(tool)
    schema = reported.get("input_schema") if reported else None
    if isinstance(schema, dict) and schema:
        return schema
    return {}


def build_endpoint_tools_payload(allowed_tools: Optional[Set[str]] = None) -> List[Dict[str, Any]]:
    allowed = {str(item).strip() for item in (allowed_tools or set()) if str(item).strip()}
    names = sorted(name for name in allowed if is_endpoint_agent_tool(name))
    return [
        {
            "type": "function",
            "function": {
                "name": name,
                "description": endpoint_tool_description(name),
                "parameters": endpoint_tool_input_schema(name),
            },
        }
        for name in names
    ]


def _parse_int(value: Any) -> Optional[int]:
    try:
        parsed = int(value)
        return parsed if parsed > 0 else None
    except Exception:
        return None


# ⚠️ DISPATCH/ROUTING ONLY — NEVER use these in system-prompt assembly.
# ``_iter_agents_for_config`` and the ``get_connected_*_agent`` resolvers read the
# in-memory ``agents`` socket registry, which is only populated in the process that
# owns the agent sockets (gateway). They are correct for *dispatching* a tool call
# to a live socket, but using them to decide what goes into the prompt makes the
# ai-runtime-built prompt diverge from the gateway-built /system-prompt-preview
# (preview shows a tool the model never actually received). For prompt-facing tool
# grants use the DB-presence helpers below (``endpoint_tools_for_config`` /
# ``endpoint_bridge_tools_for_config``). See the INVARIANT note in
# chat_runtime_helpers.build_runtime_system_prompt_and_tools.
def _agent_bound_config_ids(agent: Dict[str, Any], agent_user_id: Optional[int]) -> Set[int]:
    if agent_user_id:
        try:
            from api.devices.bindings import get_bindings

            return set(get_bindings(agent_user_id, agent.get("id")))
        except Exception:
            pass
    raw_ids = agent.get("boundAiConfigIds")
    parsed = {
        value
        for raw in (raw_ids if isinstance(raw_ids, list) else [])
        if (value := _parse_int(raw)) is not None
    }
    legacy_id = _parse_int(agent.get("aiConfigId") or agent.get("ai_config_id"))
    if legacy_id:
        parsed.add(legacy_id)
    return parsed


def _iter_agents_for_config(ai_config_id: Optional[int], user_id: Optional[int] = None):
    config_id = _parse_int(ai_config_id)
    if not config_id:
        return
    expected_user_id = _parse_int(user_id)

    for agent in list(agents.values()):
        if not isinstance(agent, dict):
            continue
        agent_user_id = _parse_int(agent.get("userId") or agent.get("user_id"))
        if config_id not in _agent_bound_config_ids(agent, agent_user_id):
            continue
        if expected_user_id and agent_user_id and agent_user_id != expected_user_id:
            continue
        yield agent


def get_connected_desktop_agent(
    ai_config_id: Optional[int], user_id: Optional[int] = None, tool: Optional[str] = None
) -> Optional[Dict[str, Any]]:
    # Android phones and custom devices are desktop-class executors for
    # dispatch: their tools route through the desktop channel, so this resolver
    # returns them too. An AI may legitimately hold one binding per type
    # (desktop + android + custom simultaneously), so when ``tool`` is given the
    # agent that actually advertises it wins over blind first-match; without a
    # tool (or when nobody advertises it) the first match keeps the old loose
    # behavior.
    tool_name = str(tool or "").strip()
    for agent in _iter_agents_for_config(ai_config_id, user_id) or []:
        if device_type_of(agent) not in ("desktop", "android", "custom"):
            continue
        if not tool_name:
            return agent
        caps = agent_endpoint_tools(agent)
        from api.devices.mcp_permissions import get_scope

        scope = get_scope(user_id, agent.get("id"), ai_config_id)
        if tool_name in caps and scope is not None and tool_name in scope:
            return agent
    return None


def get_connected_browser_agent(
    ai_config_id: Optional[int], user_id: Optional[int] = None, tool: Optional[str] = None
) -> Optional[Dict[str, Any]]:
    # An AI may hold more than one browser extension binding at once (插件 A + B),
    # each advertising its own dynamically-named tools. When ``tool`` is given the
    # extension that actually advertises it wins over blind first-match, so a call
    # to a tool that only exists on A is never dispatched to B (which would answer
    # "no such MCP"). Without a tool (or when nobody advertises it) the first
    # connected browser agent keeps the old loose behavior.
    tool_name = str(tool or "").strip()
    for agent in _iter_agents_for_config(ai_config_id, user_id) or []:
        platform = str(agent.get("platform") or "").lower()
        is_browser = bool(agent.get("isBrowserExtension")) or "browser-extension" in platform
        if not is_browser:
            continue
        if not tool_name:
            return agent
        caps = agent_endpoint_tools(agent)
        from api.devices.mcp_permissions import get_scope

        scope = get_scope(user_id, agent.get("id"), ai_config_id)
        if tool_name in caps and scope is not None and tool_name in scope:
            return agent
    return None


def get_connected_endpoint_agent(ai_config_id: Optional[int], user_id: Optional[int] = None) -> Optional[Dict[str, Any]]:
    return get_connected_desktop_agent(ai_config_id, user_id) or get_connected_browser_agent(ai_config_id, user_id)


def online_runtimes(user_id: Optional[int], device_type: str = "desktop") -> Dict[str, bool]:
    """Union of runtime availability (python/powershell/shell) across this user's
    online devices of ``device_type``. Each device reports ``runtimes`` in
    ``device:register`` (see device-side runtime-probe). Used to warn when a
    runtime tool has no online device that can actually run it."""
    out: Dict[str, bool] = {"python": False, "powershell": False, "shell": False}
    uid = _parse_int(user_id)
    for agent in list(agents.values()):
        if device_type_of(agent) != device_type:
            continue
        if uid and _parse_int(agent.get("userId") or agent.get("user_id")) != uid:
            continue
        runtimes = agent.get("runtimes")
        if not isinstance(runtimes, dict):
            continue
        for key in out:
            info = runtimes.get(key)
            if isinstance(info, dict) and info.get("available"):
                out[key] = True
    return out


def endpoint_bridge_tools_for_config(ai_config_id: Optional[int], user_id: Optional[int] = None) -> Set[str]:
    """Server bridge MCP tools granted when an endpoint executor is online.

    Resolved from the shared DB presence snapshot (``api.devices.presence``) — not
    the in-memory ``agents`` registry — so every process (gateway, ai-runtime,
    mcp-runtime, connector) returns the same answer. The in-memory registry only
    exists in the process that owns the agent sockets, so reading it here caused
    the inference path (ai-runtime, no sockets) to silently drop these tools from
    the prompt while the gateway-built preview still showed them.
    """
    config_id = _parse_int(ai_config_id)
    if not config_id:
        return set()
    from api.devices.presence import online_devices_for_config

    for _device_id, device_type, _caps in online_devices_for_config(user_id, config_id):
        if str(device_type or "").strip().lower() in ("desktop", "android", "browser", "custom"):
            return set(ENDPOINT_BRIDGE_MCP_TOOLS)
    return set()


def workshop_tools_for_config(ai_config_id: Optional[int], user_id: Optional[int] = None) -> Set[str]:
    """Workshop MCP tools available to an AI right now: the union of what its
    bound online workshop agents advertise, each narrowed by that agent's
    per-agent permission scope. 未绑定 → 空集（绑定是知识/进化工具的唯一门槛）。"""
    config_id = _parse_int(ai_config_id)
    if not config_id:
        return set()
    from api.devices.mcp_permissions import get_scope
    from api.devices.presence import online_workshop_agents_for_user
    from api.devices.workshop_bindings import workshop_device_ids_for_config

    bound_ids = set(workshop_device_ids_for_config(user_id, config_id))
    if not bound_ids:
        return set()
    tools: Set[str] = set()
    for device_id, caps in online_workshop_agents_for_user(user_id):
        if device_id not in bound_ids:
            continue
        scope = get_scope(user_id, device_id, config_id) if device_id else None
        if scope is None:
            continue
        tools |= {name for name in (caps & scope) if is_workshop_tool(name)}
    return tools


def endpoint_tools_for_config(ai_config_id: Optional[int], user_id: Optional[int] = None) -> Set[str]:
    """Endpoint MCP tools available to an AI right now.

    Each bound online endpoint contributes only the intersection of its live
    capabilities and this AI member's saved device scope. A disconnected agent
    or a member without permission contributes nothing.

    Resolved from the shared DB presence snapshot (``api.devices.presence``) so
    every process — gateway, ai-runtime, mcp-runtime, connector — gets the same
    answer without the in-memory agent registry."""
    config_id = _parse_int(ai_config_id)
    if not config_id:
        return set()
    from api.devices.presence import online_devices_for_config
    from api.devices.mcp_permissions import get_scope

    tools: Set[str] = set()
    for device_id, device_type, caps in online_devices_for_config(user_id, config_id):
        # Each individual agent has its own MCP scope.
        # No saved row (never registered) → closed for that agent.
        # Reconcile ensures full default (incl. new MCPs) on (re)connect for all types.
        scope = get_scope(user_id, device_id, config_id) if device_id else None
        if scope is None:
            continue
        tools |= caps & scope
    # 图书馆内置设备走 AI 侧绑定（兼容表 WorkshopAiBinding），与设备 1:1 绑定并集。
    tools |= workshop_tools_for_config(config_id, user_id)
    return tools


def toolbox_tools_for_config(ai_config_id: Optional[int], user_id: Optional[int] = None) -> Set[str]:
    """Re-export from tools.engine so toolbox device owns the implementation."""
    from tools.engine import toolbox_tools_for_config as _impl
    return _impl(ai_config_id, user_id)
