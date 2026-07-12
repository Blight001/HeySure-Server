from tools.introspection import _mcp_describe_tool
from fastapi import HTTPException


class _DisabledConfig:
    mcp_enabled = False
    mcp_tools = "[]"


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


def test_describe_tool_does_not_require_execution_permission(monkeypatch):
    import tools.introspection as introspection

    class _Exec:
        def where(self, *_args, **_kwargs):
            return self

        def first(self):
            return _DisabledConfig()

    class _Session:
        def __init__(self, *_args, **_kwargs):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def exec(self, *_args, **_kwargs):
            return _Exec()

    monkeypatch.setattr(introspection, "Session", _Session)

    result = _mcp_describe_tool(
        user_id=1,
        args={"tool": "workspace.search"},
        ai_config_id=123,
    )

    assert result["name"] == "workspace.search"


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


def test_describe_tool_unknown_single_tool_is_not_permission_error():
    try:
        _mcp_describe_tool(
            user_id=1,
            args={"tool": "definitely.missing_tool_for_test"},
            ai_config_id=None,
        )
    except HTTPException as exc:
        assert exc.status_code == 404
        assert str(exc.detail).startswith("Unknown MCP tool:")
        assert "not allowed" not in str(exc.detail).lower()
    else:
        raise AssertionError("expected HTTPException")
