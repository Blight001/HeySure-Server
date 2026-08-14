from tools.introspection import _mcp_describe_tool
from fastapi import HTTPException
import pytest
from types import MappingProxyType, SimpleNamespace


@pytest.fixture(autouse=True)
def _without_online_device_tools(monkeypatch):
    monkeypatch.setattr("tools.introspection.online_tool_defs_for_user", lambda _user_id: {})


def _capability(name, description="", schema=None, *, destructive=False, implementation=None):
    return SimpleNamespace(
        canonical_name=name,
        description=description,
        input_schema=MappingProxyType(schema or {}),
        implementation=(
            MappingProxyType(implementation) if implementation is not None else None
        ),
        destructive=destructive,
    )


def test_describe_tool_dedupes_same_tool_from_tool_and_tools():
    result = _mcp_describe_tool(
        user_id=1,
        args={
            "tool": "workspace.search",
            "tools": ["workspace.search"],
        },
        ai_config_id=None,
    )

    assert result["count"] == 1
    assert [tool["name"] for tool in result["tools"]] == ["workspace.search"]
    assert result["tools"][0]["requested_name"] == "workspace.search"
    assert result["errors"] == []


def test_describe_tool_dedupes_after_alias_resolution():
    result = _mcp_describe_tool(
        user_id=1,
        args={
            "tool": "workspace__search",
            "tools": ["workspace.search"],
        },
        ai_config_id=None,
    )

    assert result["count"] == 1
    assert [tool["name"] for tool in result["tools"]] == ["workspace.search"]
    assert result["tools"][0]["requested_name"] == "workspace__search"
    assert result["errors"] == []


def test_describe_tool_accepts_copied_catalog_line():
    copied_line = "workspace/workspace.search !: 联网搜索（基于 Tavily）。"

    result = _mcp_describe_tool(
        user_id=1,
        args={
            "query": "workspace.search",
            "tool": copied_line,
            "tools": [copied_line],
        },
        ai_config_id=None,
    )

    assert result["count"] == 1
    assert [tool["name"] for tool in result["tools"]] == ["workspace.search"]
    assert result["tools"][0]["requested_name"] == copied_line
    assert result["errors"] == []


def test_describe_tool_requires_current_ai_eligibility(monkeypatch):
    import tools.introspection as introspection

    monkeypatch.setattr(
        introspection,
        "_scoped_eligible_capabilities",
        lambda _user_id, _ai_config_id: {
            "mcp.describe+tool": _capability("mcp.describe+tool", "读取工具说明"),
        },
    )

    with pytest.raises(HTTPException) as raised:
        _mcp_describe_tool(
            user_id=1,
            args={"tool": "workspace.search"},
            ai_config_id=123,
        )

    assert raised.value.status_code == 404
    assert "not available" in str(raised.value.detail)


def test_describe_v2_reports_unambiguous_counts_and_next_turn(monkeypatch):
    import tools.introspection as introspection

    monkeypatch.setattr(
        introspection,
        "_scoped_eligible_capabilities",
        lambda _user_id, _ai_config_id: {
            "mcp.describe+tool": _capability("mcp.describe+tool", "读取工具说明"),
            "workspace.search": _capability("workspace.search", "联网搜索"),
        },
    )

    result = _mcp_describe_tool(
        user_id=1,
        args={"tools": ["workspace.search", "missing.tool"]},
        ai_config_id=123,
    )

    assert result["schema_version"] == 2
    assert result["request"] == {
        "mode": "batch",
        "requested_count": 2,
        "resolved_count": 1,
        "unresolved": ["missing.tool"],
    }
    assert result["count"] == 1
    assert result["count_semantics"] == "resolved_requested_tools"
    assert result["availability"]["eligible_total"] == 2
    assert result["exposure"]["callable_next_turn"] == ["workspace.search"]
    assert "Todo" in result["hint"]


def test_describe_query_only_searches_current_ai_eligible_tools(monkeypatch):
    import tools.introspection as introspection

    monkeypatch.setattr(
        introspection,
        "_scoped_eligible_capabilities",
        lambda _user_id, _ai_config_id: {
            "mcp.describe+tool": _capability("mcp.describe+tool", "读取工具说明"),
            "workspace.search": _capability(
                "workspace.search", "联网搜索", {"type": "object"}
            ),
        },
    )

    result = _mcp_describe_tool(
        user_id=1,
        args={"query": "联网搜索"},
        ai_config_id=123,
    )

    assert [item["name"] for item in result["tools"]] == ["workspace.search"]
    assert result["availability"]["eligible_total"] == 2


def test_describe_query_searches_bound_capability_description(monkeypatch):
    """Query uses the AI-scoped capability metadata, not the global registry."""
    import tools.introspection as introspection

    monkeypatch.setattr(
        introspection,
        "_scoped_eligible_capabilities",
        lambda _user_id, _ai_config_id: {
            "device.custom_flow": _capability(
                "device.custom_flow",
                "执行当前设备绑定的自动化发布流程",
                {
                    "type": "object",
                    "properties": {"title": {"type": "string"}},
                },
                destructive=True,
                implementation={"kind": "device_script"},
            ),
        },
    )

    result = _mcp_describe_tool(
        user_id=1,
        args={"query": "自动化"},
        ai_config_id=123,
    )

    assert [item["name"] for item in result["tools"]] == ["device.custom_flow"]
    assert result["tools"][0]["description"] == "执行当前设备绑定的自动化发布流程"
    assert result["tools"][0]["inputSchema"]["properties"]["title"]["type"] == "string"
    assert result["availability"] == {
        "eligible_total": 1,
        "returned_count": 1,
        "eligible_not_returned_count": 0,
    }


def test_describe_tool_accepts_browser_dot_alias_for_endpoint_tool(monkeypatch):
    import tools.introspection as introspection

    monkeypatch.setattr(
        introspection,
        "online_tool_defs_for_user",
        lambda _user_id: {
            "browser_navigate": {
                "description": "Open a URL",
                "input_schema": {
                    "type": "object",
                    "properties": {"url": {"type": "string"}},
                    "required": ["url"],
                },
                "destructive": True,
            }
        },
    )

    result = _mcp_describe_tool(
        user_id=1,
        args={"tool": "browser.navigate"},
        ai_config_id=None,
    )

    assert result["name"] == "browser_navigate"
    assert result["requested_name"] == "browser.navigate"
    assert result["inputSchema"]["required"] == ["url"]


def test_describe_tool_accepts_repeated_browser_namespace(monkeypatch):
    import tools.introspection as introspection

    monkeypatch.setattr(
        introspection,
        "online_tool_defs_for_user",
        lambda _user_id: {
            "browser_navigate": {
                "description": "Open a URL",
                "input_schema": {"type": "object", "properties": {}},
                "destructive": True,
            }
        },
    )

    result = _mcp_describe_tool(
        user_id=1,
        args={"tool": "browser.browser_navigate"},
        ai_config_id=None,
    )

    assert result["name"] == "browser_navigate"
    assert result["requested_name"] == "browser.browser_navigate"


def test_describe_tool_includes_knowledge_manage(monkeypatch):
    import tools.introspection as introspection

    monkeypatch.setattr(introspection, "online_tool_defs_for_user", lambda _user_id: {})

    result = _mcp_describe_tool(
        user_id=1,
        args={"tool": "knowledge.manage"},
        ai_config_id=None,
    )

    assert result["name"] == "knowledge.manage"
    assert "action" in (result.get("inputSchema") or {}).get("properties", {})
    assert len(result.get("schemaVersion") or "") == 16


def test_describe_tool_unknown_single_tool_uses_non_enumerating_error():
    try:
        _mcp_describe_tool(
            user_id=1,
            args={"tool": "definitely.missing_tool_for_test"},
            ai_config_id=None,
        )
    except HTTPException as exc:
        assert exc.status_code == 404
        assert str(exc.detail).startswith("MCP tool is not available:")
    else:
        raise AssertionError("expected HTTPException")
