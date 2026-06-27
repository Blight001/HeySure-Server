"""Group MCP tools for front-prompt preview: workspace (server) vs per-device."""

from typing import Any, Dict, List, Optional, Set

from api.devices.live import connected_agent_rows_for_user
from api.devices.mcp_permissions import get_scope
from connector_runtime.dispatch.desktop_device_tools import (
    _config_selected_tool_names,
    _iter_agents_for_config,
    _parse_int,
    agent_endpoint_tools,
    device_type_of,
    is_endpoint_agent_tool,
)


def _is_workspace_tool(tool: Dict[str, Any]) -> bool:
    return str(tool.get("mcpSource") or "server").strip() == "server"


def _agent_display_name(agent: Dict[str, Any]) -> str:
    name = str(agent.get("name") or agent.get("deviceName") or agent.get("id") or "").strip()
    if name:
        return name
    device_type = device_type_of(agent)
    if device_type == "browser":
        return "浏览器端"
    if device_type == "android":
        return "安卓端"
    if device_type == "workshop":
        return "图书馆"
    return "桌面端"


def _agents_for_prompt_groups(user_id: int, ai_config_id: Optional[int]) -> List[Dict[str, Any]]:
    if ai_config_id is not None:
        agents = [agent for agent in _iter_agents_for_config(ai_config_id, user_id) if isinstance(agent, dict)]
        seen = {str(agent.get("id") or "").strip() for agent in agents if str(agent.get("id") or "").strip()}
        for agent in connected_agent_rows_for_user(user_id):
            if not isinstance(agent, dict) or device_type_of(agent) != "workshop":
                continue
            bound_cfg = _parse_int(agent.get("aiConfigId") or agent.get("ai_config_id"))
            if bound_cfg != ai_config_id:
                continue
            device_id = str(agent.get("id") or "").strip()
            if device_id and device_id not in seen:
                agents.append(agent)
                seen.add(device_id)
        return agents
    return [
        agent for agent in connected_agent_rows_for_user(user_id)
        if isinstance(agent, dict) and device_type_of(agent) in {"desktop", "browser", "android", "workshop"}
    ]


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