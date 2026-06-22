import json

from api.services.mcp_prompt_groups import build_prompt_tool_groups
from mcp_runtime.mcp.permissions import LIBRARY_BOUND_TOOLS, clamp_tools_json


class _User:
    id = 1
    role_mcp_permissions = json.dumps({
        "digital_member_member": ["workspace.search"],
    })


def _prompt_tools():
    from mcp_runtime.mcp import registry

    return [
        {
            **tool,
            "mcpSource": "server",
        }
        for tool in registry.list_tools()
        if str(tool.get("name") or "").strip()
    ]


def test_clamp_tools_json_keeps_library_bound_tools_despite_role_policy():
    requested = json.dumps(sorted(LIBRARY_BOUND_TOOLS), ensure_ascii=False)
    clamped = json.loads(
        clamp_tools_json(_User(), "digital_member_member", requested)
    )
    assert set(clamped) == set(LIBRARY_BOUND_TOOLS)


def test_build_prompt_tool_groups_includes_governance_tools(monkeypatch):
    monkeypatch.setattr(
        "api.services.mcp_prompt_groups._config_selected_tool_names",
        lambda ai_config_id, user_id: set(LIBRARY_BOUND_TOOLS),
    )
    monkeypatch.setattr(
        "api.services.mcp_prompt_groups._agents_for_prompt_groups",
        lambda user_id, ai_config_id: [{
            "id": "workshop-user-1",
            "name": "图书馆",
            "isWorkshop": True,
            "capabilities": [],
        }],
    )

    allowed = set(LIBRARY_BOUND_TOOLS) | {"workspace.search"}
    groups = build_prompt_tool_groups(
        user_id=1,
        ai_config_id=42,
        prompt_tools=_prompt_tools(),
        allowed_tools=allowed,
    )
    library_group = next(group for group in groups if group.get("groupKey") == "library")
    names = {tool["name"] for tool in library_group["tools"]}
    assert LIBRARY_BOUND_TOOLS.issubset(names)