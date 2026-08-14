import asyncio
import json
from types import SimpleNamespace

from api.models import AssistantAIConfig
from gateway.routers import mcp


class _QueryResult:
    def __init__(self, value):
        self._value = value

    def first(self):
        return self._value


class _Session:
    def __init__(self, value):
        self._value = value

    def exec(self, _statement):
        return _QueryResult(self._value)


def test_gateway_endpoint_call_uses_runtime_aware_dispatch(monkeypatch):
    tool = "aifree.windows+tab"
    cfg = AssistantAIConfig(
        id=19,
        user_id=1,
        name="external-member",
        mcp_enabled=True,
        mcp_tools=json.dumps([tool]),
    )
    user = SimpleNamespace(id=1)
    observed = {}

    async def fake_dispatch(tool_name, user_id, arguments, ai_config_id):
        observed["call"] = (tool_name, user_id, arguments, ai_config_id)
        return {
            "tool": tool_name,
            "destructive": True,
            "result": {"success": True},
        }

    monkeypatch.setattr(mcp, "get_current_user", lambda _authorization, _session: user)
    monkeypatch.setattr(mcp, "endpoint_tools_for_config", lambda *_args: {tool})
    monkeypatch.setattr(mcp, "endpoint_bridge_tools_for_config", lambda *_args: set())
    monkeypatch.setattr(
        "api.services.mcp.capability_view.ensure_tool_eligible",
        lambda *_args: None,
    )
    monkeypatch.setattr(mcp, "is_endpoint_agent_tool", lambda name: name == tool)
    monkeypatch.setattr(mcp.registry, "has", lambda _name: False)
    monkeypatch.setattr(mcp.registry, "list_tools", lambda: [])
    monkeypatch.setattr(mcp, "call_mcp_or_endpoint_tool", fake_dispatch)

    result = asyncio.run(
        mcp.call_mcp_tool(
            mcp.MCPCallRequest(
                tool=tool,
                arguments={"action": "list"},
                ai_config_id=19,
            ),
            session=_Session(cfg),
            authorization="Bearer test",
        )
    )

    assert observed["call"] == (tool, 1, {"action": "list"}, 19)
    assert result["result"]["success"] is True
