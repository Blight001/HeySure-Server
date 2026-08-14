import json
from types import SimpleNamespace

from api.chat_runtime.chat_prompt_utils import _filter_tools_for_current_bindings
from api.services.mcp.mcp_prompt_groups import build_prompt_tool_groups
from mcp_runtime.mcp.permissions import (
    LIBRARY_BOUND_TOOLS,
    requires_library_binding,
)
from tools.engine import is_toolbox_gated_tool, toolbox_capability_names


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


def test_member_manage_belongs_to_library_instead_of_toolbox():
    assert "member.manage" in LIBRARY_BOUND_TOOLS
    assert requires_library_binding("member.manage") is True
    assert is_toolbox_gated_tool("member.manage") is False
    assert "member.manage" not in toolbox_capability_names()


def test_library_actions_do_not_depend_on_member_roles():
    from tools.knowledge import _KNOWLEDGE_ACTIONS
    from tools.members import MEMBER_ACTIONS, MEMBER_MANAGE_SCHEMA, TASK_ACTIONS
    from tools.tasks import _TASK_ACTIONS

    assert all(callable(handler) for handler in _KNOWLEDGE_ACTIONS.values())
    assert all(callable(handler) for handler in _TASK_ACTIONS.values())
    assert set(MEMBER_MANAGE_SCHEMA["properties"]["action"]["enum"]) == set(MEMBER_ACTIONS + TASK_ACTIONS)
    assert "delete" not in MEMBER_MANAGE_SCHEMA["properties"]["action"]["enum"]


def test_unbound_ai_loses_member_manage_but_keeps_todo_tool(monkeypatch):
    monkeypatch.setattr(
        "api.services.mcp.capability_view.scoped_tool_view_for_ids",
        lambda user_id, ai_config_id: SimpleNamespace(eligible_names={"todo.manage"}),
    )

    filtered = _filter_tools_for_current_bindings(
        {"member.manage", "todo.manage"},
        user_id=1,
        ai_config_id=42,
    )

    assert "member.manage" not in filtered
    assert "todo.manage" in filtered


def test_build_prompt_tool_groups_includes_governance_tools(monkeypatch):
    monkeypatch.setattr(
        "api.services.mcp.mcp_prompt_groups._agents_for_prompt_groups",
        lambda user_id, ai_config_id: [{
            "id": "workshop-user-1",
            "name": "图书馆",
            "isWorkshop": True,
            "capabilities": [],
        }],
    )
    # Ensure the library group is not cleared by the bound check inside the function.
    monkeypatch.setattr(
        "api.devices.workshop_bindings.config_bound_to_library",
        lambda user_id, ai_config_id: True,
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
    assert "member.manage" in names
    toolbox_group = next(group for group in groups if group.get("groupKey") == "toolbox")
    assert "member.manage" not in {tool["name"] for tool in toolbox_group["tools"]}


def test_removed_duplicate_library_tools_are_not_registered():
    from mcp_runtime.mcp import registry

    names = {item["name"] for item in registry.list_tools()}
    assert "member.manage" in names
    assert {"admin.manage", "task.manage", "prompt.manage"}.isdisjoint(names)


def test_removed_duplicate_library_tools_are_cleaned_from_saved_configs():
    from api.services.mcp.mcp_tool_aliases import fully_clean_tool_names

    assert fully_clean_tool_names({
        "member.manage", "admin.manage", "task.manage", "prompt.manage"
    }) == {"member.manage"}


def test_browser_local_card_tools_are_hidden_in_favor_of_server_automation():
    from api.services.mcp.mcp_tool_aliases import fully_clean_tool_names

    assert fully_clean_tool_names({
        "automation.manage", "manage_card", "run_card", "write_card",
    }) == {"automation.manage"}


def test_task_runtime_does_not_regrant_member_management():
    from api.services.tasks.task_system import TASK_RUNTIME_REQUIRED_TOOLS

    assert "member.manage" not in TASK_RUNTIME_REQUIRED_TOOLS
