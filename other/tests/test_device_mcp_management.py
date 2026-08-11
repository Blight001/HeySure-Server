import asyncio
from unittest.mock import AsyncMock

from tools import device_mcp


def _devices():
    return [
        {
            "id": "linux-abc-123",
            "name": "linux-server",
            "remark": "生产服务器",
            "deviceType": "linux",
            "platform": "opencloudos",
            "online": True,
            "aiConfigId": 9,
            "capabilities": ["fs.read", "shell.run", "remote_terminal"],
            "toolDefs": {
                "fs.read": {"description": "读取文件", "input_schema": {"type": "object"}},
                "shell.run": {"description": "运行命令", "destructive": True},
            },
        },
        {
            "id": "browser-offline",
            "name": "Chrome",
            "deviceType": "browser",
            "online": False,
            "capabilities": ["browser_tab"],
        },
    ]


def test_devices_returns_device_number_for_member_binding(monkeypatch):
    monkeypatch.setattr(device_mcp, "connected_agent_rows_for_user", lambda _user_id: _devices())
    monkeypatch.setattr(device_mcp, "get_scope", lambda *_args: None)

    result = asyncio.run(device_mcp._device_mcp_manage(1, {"action": "devices"}, 2))

    assert result["ok"] is True
    assert result["devices"][0]["deviceId"] == "linux-abc-123"
    assert result["devices"][0]["deviceNumber"] == "linux-abc-123"
    assert result["devices"][0]["availableMcpCount"] == 2
    assert "member.manage" in result["bindingHint"]


def test_scope_get_filters_transport_capabilities(monkeypatch):
    monkeypatch.setattr(device_mcp, "connected_agent_rows_for_user", lambda _user_id: _devices())
    monkeypatch.setattr(device_mcp, "get_scope", lambda *_args: {"fs.read"})

    result = asyncio.run(device_mcp._device_mcp_manage(
        1,
        {"action": "scope_get", "device_id": "linux-abc-123"},
        2,
    ))

    assert result["capabilities"] == ["fs.read", "shell.run"]
    assert result["allowed"] == ["fs.read"]
    assert result["tools"][0]["description"] == "读取文件"


def test_scope_set_rejects_unknown_tool_without_writing(monkeypatch):
    monkeypatch.setattr(device_mcp, "connected_agent_rows_for_user", lambda _user_id: _devices())
    monkeypatch.setattr(device_mcp, "get_scope", lambda *_args: {"fs.read"})
    save = AsyncMock()
    monkeypatch.setattr(device_mcp, "emit_agent_list_for_user", save)
    writes = []
    monkeypatch.setattr(device_mcp, "set_scope", lambda *args, **kwargs: writes.append((args, kwargs)))

    result = asyncio.run(device_mcp._device_mcp_manage(
        1,
        {"action": "scope_set", "device_id": "linux-abc-123", "tools": ["missing.tool"]},
        2,
    ))

    assert result["ok"] is False
    assert result["unknownTools"] == ["missing.tool"]
    assert writes == []
    save.assert_not_awaited()


def test_scope_set_persists_exact_allowlist_and_refreshes_ui(monkeypatch):
    monkeypatch.setattr(device_mcp, "connected_agent_rows_for_user", lambda _user_id: _devices())
    scopes = {"linux-abc-123": {"fs.read", "shell.run"}}
    monkeypatch.setattr(device_mcp, "get_scope", lambda _user_id, device_id, *_args: scopes.get(device_id))

    def save_scope(_user_id, device_id, tools, **_kwargs):
        scopes[device_id] = set(tools)
        return scopes[device_id]

    refreshed = AsyncMock()
    monkeypatch.setattr(device_mcp, "set_scope", save_scope)
    monkeypatch.setattr(device_mcp, "emit_agent_list_for_user", refreshed)

    result = asyncio.run(device_mcp._device_mcp_manage(
        1,
        {"action": "scope_set", "device_id": "linux-abc-123", "tools": ["shell.run"]},
        2,
    ))

    assert result["ok"] is True
    assert result["allowed"] == ["shell.run"]
    refreshed.assert_awaited_once_with(1)


def test_dynamic_tool_actions_still_require_device_type():
    result = asyncio.run(device_mcp._device_mcp_manage(1, {"action": "list"}, 2))
    assert result["ok"] is False
    assert "device_type" in result["error"]


def test_schema_only_requires_action_globally():
    assert device_mcp.DEVICE_MCP_MANAGE_SCHEMA["required"] == ["action"]
    assert {"devices", "scope_get", "scope_set"}.issubset(
        set(device_mcp.DEVICE_MCP_MANAGE_SCHEMA["properties"]["action"]["enum"])
    )
