import json

from api.services.mcp.mcp_prompt_groups import build_prompt_tool_groups
from mcp_runtime.mcp.permissions import (
    LIBRARY_BOUND_TOOLS,
    clamp_tools_json,
    effective_allowed_for_tier,
)


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


def test_mode_manage_allowed_despite_restrictive_role_policy():
    """基础对话控制工具 mode.manage 不受角色策略白名单收敛：即使管理员保存的
    per-role 策略里没有它，运行时天花板也必须放行——否则用户无法在对话「+」面板
    切换模式（前端走同一条 /api/mcp/call 的角色天花板校验）。"""
    from mcp_runtime.mcp import registry

    names = {
        str(t.get("name") or "").strip()
        for t in registry.list_tools()
        if str(t.get("name") or "").strip()
    }
    assert "mode.manage" in names  # 前提：确是已注册工具
    allowed = effective_allowed_for_tier(_User(), "digital_member_member", names)
    assert "mode.manage" in allowed
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