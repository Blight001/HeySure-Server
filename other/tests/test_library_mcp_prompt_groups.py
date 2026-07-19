import json

from api.chat_runtime.chat_prompt_utils import _filter_tools_for_current_bindings
from api.services.mcp.mcp_prompt_groups import build_prompt_tool_groups
from mcp_runtime.mcp.permissions import (
    LIBRARY_BOUND_TOOLS,
    ROLE_MEMBER,
    clamp_tools_json,
    effective_allowed_for_tier,
    requires_library_binding,
    tool_min_role,
)
from tools.engine import is_toolbox_gated_tool, toolbox_capability_names


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


def test_task_manage_belongs_to_library_instead_of_toolbox():
    assert "task.manage" in LIBRARY_BOUND_TOOLS
    assert requires_library_binding("task.manage") is True
    assert is_toolbox_gated_tool("task.manage") is False
    assert "task.manage" not in toolbox_capability_names()


def test_library_binding_replaces_all_role_gates():
    """Every library MCP and every folded action has the member floor.

    Runtime binding checks are covered separately; this locks down the policy
    that a bound AI's identity never turns a library read/write into a 403.
    """
    from tools.knowledge import _KNOWLEDGE_ACTIONS
    from tools.prompts import _PROMPT_ACTIONS
    from tools.tasks import _TASK_ACTIONS

    assert all(tool_min_role(name) == ROLE_MEMBER for name in LIBRARY_BOUND_TOOLS)
    assert all(callable(handler) for handler in _PROMPT_ACTIONS.values())
    assert all(callable(handler) for handler in _KNOWLEDGE_ACTIONS.values())
    assert all(callable(handler) for handler in _TASK_ACTIONS.values())


def test_unbound_ai_loses_task_manage_but_keeps_todo_tool(monkeypatch):
    monkeypatch.setattr(
        "api.devices.workshop_bindings.config_bound_to_library",
        lambda user_id, ai_config_id: False,
    )

    filtered = _filter_tools_for_current_bindings(
        {"task.manage", "todo.manage"},
        user_id=1,
        ai_config_id=42,
    )

    assert "task.manage" not in filtered
    assert "todo.manage" in filtered


def test_describe_tool_allowed_despite_restrictive_role_policy():
    """自省工具 mcp.describe+tool 不受角色策略白名单收敛：即使管理员保存的
    per-role 策略里没有它，运行时天花板也必须放行。"""
    from mcp_runtime.mcp import registry

    names = {
        str(t.get("name") or "").strip()
        for t in registry.list_tools()
        if str(t.get("name") or "").strip()
    }
    allowed = effective_allowed_for_tier(_User(), "digital_member_member", names)
    assert "mcp.describe+tool" in allowed


def test_build_prompt_tool_groups_includes_governance_tools(monkeypatch):
    monkeypatch.setattr(
        "api.services.mcp.mcp_prompt_groups._config_selected_tool_names",
        lambda ai_config_id, user_id: set(LIBRARY_BOUND_TOOLS),
    )
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
    assert "task.manage" in names
    toolbox_group = next(group for group in groups if group.get("groupKey") == "toolbox")
    assert "task.manage" not in {tool["name"] for tool in toolbox_group["tools"]}
