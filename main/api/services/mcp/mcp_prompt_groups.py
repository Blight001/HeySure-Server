"""Group MCP tools for front-prompt preview: workspace (server) vs per-device.

These groups are rendered into the SYSTEM PROMPT (via
``chat_prompt_utils._build_dynamic_mcp_explanation``), so this module runs in
BOTH the gateway (live ``/system-prompt-preview``) and the ai-runtime worker
(the prompt the model actually receives).

⚠️ HARD RULE — every device/tool source here MUST be PROCESS-INDEPENDENT
(resolved from the DB: ``api.devices.presence`` + ``api.devices.mcp_permissions``).
NEVER read the in-memory ``api.sio.agents`` socket registry or the
``connector_runtime.dispatch.desktop_device_tools._iter_agents_for_config`` /
``get_connected_*_agent`` resolvers built on it: that registry only exists in the
gateway process, so doing so makes the ai-runtime prompt silently DROP every
device group while the gateway preview still shows them (the exact bug this
module was hardened against). See the INVARIANT note in
``chat_runtime_helpers.build_runtime_system_prompt_and_tools`` and the regression
test ``other/tests/test_prompt_groups_db_backed.py``.
"""

from typing import Any, Dict, List, Optional, Set

from api.devices.mcp_permissions import get_scope
from connector_runtime.dispatch.desktop_device_tools import (
    _config_selected_tool_names,
    agent_endpoint_tools,
    device_type_of,
    is_endpoint_agent_tool,
)


def _is_workspace_tool(tool: Dict[str, Any]) -> bool:
    return str(tool.get("mcpSource") or "server").strip() == "server"


def _agent_display_name(agent: Dict[str, Any]) -> str:
    # 只用用户可读的设备名；未起名的设备 name 往往回落成设备编号
    # （如 br-mh4a3wc0），编号对用户和模型都没有信息量，一律改用
    # 设备类型的友好名称展示（浏览器插件 / 桌面端 / 安卓端 / 图书馆）。
    device_id = str(agent.get("id") or agent.get("deviceId") or "").strip()
    name = str(agent.get("name") or agent.get("deviceName") or "").strip()
    if name and name.lower() != device_id.lower():
        return name
    device_type = device_type_of(agent)
    if device_type == "browser":
        return "浏览器插件"
    if device_type == "android":
        return "安卓端"
    if device_type == "workshop":
        return "图书馆"
    return "桌面端"


_PRESENCE_TYPE_FLAG = {
    "browser": "isBrowserExtension",
    "android": "isAndroid",
    "desktop": "isWindowsDesktop",
    "workshop": "isWorkshop",
}


def _presence_agent_dict(device_id: str, device_type: str, caps) -> Dict[str, Any]:
    """Synthesize the agent-like record the group builder expects from a DB
    presence row, so ``device_type_of`` / ``agent_endpoint_tools`` keep working
    without the in-memory socket registry."""
    agent: Dict[str, Any] = {
        "id": device_id,
        "platform": device_type,
        "capabilities": sorted({str(c).strip() for c in (caps or []) if str(c).strip()}),
    }
    flag = _PRESENCE_TYPE_FLAG.get(str(device_type or "").strip())
    if flag:
        agent[flag] = True
    return agent


def _agents_for_prompt_groups(user_id: int, ai_config_id: Optional[int]) -> List[Dict[str, Any]]:
    """Endpoint agents to render as device groups, built from the **DB presence
    snapshot** (process-independent) — NOT the in-memory ``agents`` socket
    registry, which only exists in the gateway process. Reading that registry here
    made the ai-runtime-built prompt drop every device group (it owns no sockets)
    while the gateway-built /system-prompt-preview still showed them. See the
    INVARIANT note in chat_runtime_helpers.build_runtime_system_prompt_and_tools."""
    from api.devices.presence import online_devices_for_config, online_tool_catalog_for_user

    agents: List[Dict[str, Any]] = []
    seen: Set[str] = set()
    if ai_config_id is not None:
        for device_id, device_type, caps in online_devices_for_config(user_id, ai_config_id):
            did = str(device_id or "").strip()
            if not did or did in seen:
                continue
            seen.add(did)
            agents.append(_presence_agent_dict(did, str(device_type or "").strip(), caps))
        return agents
    for entry in online_tool_catalog_for_user(user_id):
        did = str(entry.get("device_id") or "").strip()
        if not did or did in seen:
            continue
        seen.add(did)
        caps = [str(t.get("name") or "").strip() for t in (entry.get("tools") or [])]
        agents.append(_presence_agent_dict(did, str(entry.get("device_type") or "").strip(), caps))
    return agents


def _tool_names_for_agent(
    agent: Dict[str, Any],
    *,
    user_id: int,
    ai_config_id: Optional[int],
    allowed_tools: Optional[Set[str]],
) -> Set[str]:
    device_id = str(agent.get("id") or "").strip()
    caps = agent_endpoint_tools(agent)
    scope = get_scope(user_id, device_id) if device_id else None
    names: Set[str] = set()
    if scope is not None:
        names |= caps & scope
    if ai_config_id is not None:
        names |= _config_selected_tool_names(ai_config_id, user_id) & caps
    if allowed_tools is not None:
        names &= allowed_tools
    return {name for name in names if is_endpoint_agent_tool(name)}


def build_prompt_tool_groups(
    *,
    user_id: int,
    ai_config_id: Optional[int],
    prompt_tools: List[Dict[str, Any]],
    allowed_tools: Optional[Set[str]],
) -> List[Dict[str, Any]]:
    by_name: Dict[str, Dict[str, Any]] = {}
    for tool in prompt_tools:
        name = str(tool.get("name") or "").strip()
        if name:
            by_name[name] = tool

    workspace_names: Set[str] = set()
    if allowed_tools is None:
        workspace_names = {name for name, tool in by_name.items() if _is_workspace_tool(tool)}
    else:
        workspace_names = {
            name for name in allowed_tools
            if name in by_name and _is_workspace_tool(by_name[name])
        }

    # 工作区（服务端）MCP 再分两组：工具箱（默认即用）与 图书馆（需绑定图书馆）。
    from mcp_runtime.mcp.permissions import LIBRARY_BOUND_TOOLS

    toolbox_tools = [
        by_name[name]
        for name in sorted(workspace_names)
        if name in by_name and name not in LIBRARY_BOUND_TOOLS
    ]
    library_tool_names: Set[str] = {
        name for name in sorted(workspace_names)
        if name in by_name and name in LIBRARY_BOUND_TOOLS
    }
    # 治理类 manage 工具只存于 AI 配置的 mcp_tools；显式并入图书馆分组。
    if ai_config_id is not None:
        library_tool_names |= _config_selected_tool_names(ai_config_id, user_id) & LIBRARY_BOUND_TOOLS
    if allowed_tools is not None:
        library_tool_names |= {name for name in allowed_tools if name in LIBRARY_BOUND_TOOLS}

    # Only expose the library group if this specific AI is actually bound to the library.
    # Otherwise the "图书馆 MCP" group leaks even when not connected.
    if ai_config_id is not None:
        try:
            from api.devices.workshop_bindings import config_bound_to_library
            if not config_bound_to_library(user_id, ai_config_id):
                library_tool_names = set()
        except Exception:
            pass
    groups: List[Dict[str, Any]] = [{
        "groupKey": "toolbox",
        "groupLabel": "工具箱 MCP",
        "groupKind": "workspace",
        "tools": toolbox_tools,
    }]

    agents = _agents_for_prompt_groups(user_id, ai_config_id)
    for agent in agents:
        device_id = str(agent.get("id") or "").strip()
        if not device_id:
            continue
        agent_type = device_type_of(agent)
        names = _tool_names_for_agent(
            agent,
            user_id=user_id,
            ai_config_id=ai_config_id,
            allowed_tools=allowed_tools,
        )
        if agent_type == "workshop":
            continue
        device_tools: List[Dict[str, Any]] = []
        for name in sorted(names):
            tool = by_name.get(name)
            if tool:
                device_tools.append(tool)
                continue
            device_tools.append({
                "name": name,
                "description": "",
                "inputSchema": {},
                "destructive": True,
                "mcpSource": str(agent_type or "desktop"),
                "deviceId": device_id,
                "allowedForCurrentAi": True,
            })
        groups.append({
            "groupKey": f"device:{device_id}",
            "groupLabel": f"{_agent_display_name(agent)} MCP",
            "groupKind": "device",
            "deviceId": device_id,
            "deviceType": str(agent_type or ""),
            "tools": device_tools,
        })

    library_tools: List[Dict[str, Any]] = []
    for name in sorted(library_tool_names):
        tool = by_name.get(name)
        if tool:
            library_tools.append(tool)
            continue
        library_tools.append({
            "name": name,
            "description": "",
            "inputSchema": {},
            "destructive": True,
            "mcpSource": "workshop",
            "allowedForCurrentAi": True,
        })
    if library_tools:
        groups.append({
            "groupKey": "library",
            "groupLabel": "图书馆 MCP",
            "groupKind": "workspace",
            "tools": library_tools,
        })

    if not agents:
        groups.append({
            "groupKey": "device:none",
            "groupLabel": "端侧设备 MCP",
            "groupKind": "device",
            "deviceId": "",
            "deviceType": "",
            "tools": [],
        })

    return groups
