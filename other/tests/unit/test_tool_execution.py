import asyncio

from ai_runtime.inference import tool_execution


def test_call_routes_regular_tool_to_split_mcp_runtime(monkeypatch):
    observed = {}

    async def fake_runtime_call(runtime_url, tool, user_id, arguments, ai_config_id):
        observed["call"] = (runtime_url, tool, user_id, arguments, ai_config_id)
        return {"result": {"success": True}}

    monkeypatch.setattr(tool_execution, "is_workshop_tool", lambda _tool: False)
    monkeypatch.setattr(tool_execution, "is_endpoint_agent_tool", lambda _tool: False)
    monkeypatch.setattr(tool_execution.settings, "mcp_runtime_url", "http://mcp:3001")
    monkeypatch.setattr(tool_execution, "call_mcp_via_runtime", fake_runtime_call)

    result = asyncio.run(
        tool_execution.call_mcp_or_endpoint_tool(
            "workspace.read",
            9,
            {"path": "README.md"},
            3,
        )
    )

    assert result["result"]["success"] is True
    assert observed["call"] == (
        "http://mcp:3001",
        "workspace.read",
        9,
        {"path": "README.md"},
        3,
    )


def test_call_keeps_workspace_search_in_process(monkeypatch):
    observed = {}

    async def fake_registry_call(tool, user_id, arguments, ai_config_id):
        observed["tool"] = tool
        return {"result": {"success": True, "items": []}}

    async def unexpected_runtime_call(*args, **kwargs):
        raise AssertionError("workspace.search must not use split MCP runtime")

    monkeypatch.setattr(tool_execution, "is_workshop_tool", lambda _tool: False)
    monkeypatch.setattr(tool_execution, "is_endpoint_agent_tool", lambda _tool: False)
    monkeypatch.setattr(tool_execution.settings, "mcp_runtime_url", "http://mcp:3001")
    monkeypatch.setattr(tool_execution.registry, "call", fake_registry_call)
    monkeypatch.setattr(tool_execution, "call_mcp_via_runtime", unexpected_runtime_call)

    result = asyncio.run(
        tool_execution.call_mcp_or_endpoint_tool("workspace.search", 9, {"q": "x"}, None)
    )

    assert observed["tool"] == "workspace.search"
    assert result["result"]["items"] == []


def test_call_routes_endpoint_tool_to_connector_runtime(monkeypatch):
    observed = {}

    async def fake_runtime_dispatch(
        runtime_url,
        tool,
        user_id,
        arguments,
        ai_config_id,
        timeout_seconds,
    ):
        observed["call"] = (
            runtime_url,
            tool,
            user_id,
            arguments,
            ai_config_id,
            timeout_seconds,
        )
        return {"success": True, "result": {"total": 1}}

    async def unexpected_in_process_dispatch(**_kwargs):
        raise AssertionError("split Gateway must not use its in-process agent registry")

    monkeypatch.setattr(tool_execution, "is_workshop_tool", lambda _tool: False)
    monkeypatch.setattr(tool_execution, "is_endpoint_agent_tool", lambda _tool: True)
    monkeypatch.setattr(tool_execution.settings, "service_role", "gateway")
    monkeypatch.setattr(
        tool_execution.settings,
        "connector_runtime_url",
        "http://connector-runtime:3002",
    )
    monkeypatch.setattr(tool_execution.settings, "api_gateway_url", "http://api-gateway:3000")
    monkeypatch.setattr(tool_execution, "endpoint_dispatch_timeout", lambda *_args: 120)
    monkeypatch.setattr(tool_execution, "dispatch_endpoint_via_runtime", fake_runtime_dispatch)
    monkeypatch.setattr(
        tool_execution,
        "dispatch_endpoint_in_process",
        unexpected_in_process_dispatch,
    )

    result = asyncio.run(
        tool_execution.call_mcp_or_endpoint_tool(
            "aifree.windows+tab",
            1,
            {"action": "list"},
            19,
        )
    )

    assert observed["call"] == (
        "http://connector-runtime:3002",
        "aifree.windows+tab",
        1,
        {"action": "list"},
        19,
        120,
    )
    assert result["result"]["success"] is True


def test_call_normalizes_native_card_steps_before_connector_dispatch(monkeypatch):
    observed = {}

    async def fake_runtime_dispatch(
        runtime_url,
        tool,
        user_id,
        arguments,
        ai_config_id,
        timeout_seconds,
    ):
        observed["arguments"] = arguments
        return {"success": True}

    monkeypatch.setattr(tool_execution, "is_workshop_tool", lambda _tool: False)
    monkeypatch.setattr(tool_execution, "is_endpoint_agent_tool", lambda _tool: True)
    monkeypatch.setattr(tool_execution.settings, "service_role", "gateway")
    monkeypatch.setattr(
        tool_execution.settings,
        "connector_runtime_url",
        "http://connector-runtime:3002",
    )
    monkeypatch.setattr(tool_execution.settings, "api_gateway_url", "http://api-gateway:3000")
    monkeypatch.setattr(tool_execution, "endpoint_dispatch_timeout", lambda *_args: 120)
    monkeypatch.setattr(tool_execution, "dispatch_endpoint_via_runtime", fake_runtime_dispatch)

    asyncio.run(
        tool_execution.call_mcp_or_endpoint_tool(
            "aifree.manage+card",
            1,
            {
                "action": "write",
                "cardData": {
                    "steps": [
                        {"type": "mcp", "tool": "aifree.browser+action"},
                    ],
                },
            },
            4,
        )
    )

    assert observed["arguments"]["cardData"]["steps"][0]["tool"] == "browser_action"


def test_registered_server_tool_cannot_be_shadowed_by_device(monkeypatch):
    observed = {}

    async def fake_runtime_call(runtime_url, tool, user_id, arguments, ai_config_id):
        observed["call"] = (runtime_url, tool, user_id, arguments, ai_config_id)
        return {"result": {"success": True, "source": "toolbox"}}

    async def unexpected_device_dispatch(**_kwargs):
        raise AssertionError("a registered server tool must not dispatch to a device")

    monkeypatch.setattr(tool_execution, "is_workshop_tool", lambda _tool: False)
    monkeypatch.setattr(tool_execution, "is_endpoint_agent_tool", lambda _tool: True)
    monkeypatch.setattr(tool_execution.registry, "has", lambda name: name == "automation.manage")
    monkeypatch.setattr(tool_execution.settings, "mcp_runtime_url", "http://mcp:3001")
    monkeypatch.setattr(tool_execution, "call_mcp_via_runtime", fake_runtime_call)
    monkeypatch.setattr(tool_execution, "dispatch_endpoint_in_process", unexpected_device_dispatch)

    result = asyncio.run(tool_execution.call_mcp_or_endpoint_tool(
        "automation.manage", 1, {"action": "list"}, 7,
    ))

    assert result["result"]["source"] == "toolbox"
    assert observed["call"][1] == "automation.manage"


def test_execute_tool_call_returns_normalized_success(monkeypatch):
    async def fake_call(tool, user_id, arguments, ai_config_id):
        return {"tool": tool, "result": {"success": True, "value": 7}}

    monkeypatch.setattr(tool_execution, "call_mcp_or_endpoint_tool", fake_call)
    monkeypatch.setattr(tool_execution, "is_endpoint_agent_tool", lambda _tool: False)
    monkeypatch.setattr(
        tool_execution,
        "run_async",
        lambda awaitable, timeout=None: asyncio.run(awaitable),
    )

    execution = tool_execution.execute_tool_call(
        "workspace.read",
        9,
        {"path": "README.md"},
        3,
    )

    assert execution.failed is False
    assert execution.error == ""
    assert execution.result["result"]["value"] == 7
    assert '"value": 7' in execution.display_text
    assert execution.latency >= 0


def test_execute_tool_call_uses_extended_endpoint_bridge_timeout(monkeypatch):
    observed = {}

    async def fake_call(tool, user_id, arguments, ai_config_id):
        return {"result": {"success": False, "error": "device offline"}}

    def fake_run_async(awaitable, timeout=None):
        observed["timeout"] = timeout
        return asyncio.run(awaitable)

    monkeypatch.setattr(tool_execution, "call_mcp_or_endpoint_tool", fake_call)
    monkeypatch.setattr(tool_execution, "is_endpoint_agent_tool", lambda _tool: True)
    monkeypatch.setattr(tool_execution, "endpoint_dispatch_timeout", lambda _tool, _args: 420)
    monkeypatch.setattr(tool_execution, "run_async", fake_run_async)

    execution = tool_execution.execute_tool_call("run_card", 9, {}, None)

    assert observed["timeout"] == 450
    assert execution.failed is True
    assert execution.error == "device offline"
    assert "device offline" in execution.display_text


def test_execute_tool_call_converts_bridge_exception_to_result(monkeypatch):
    async def fake_call(tool, user_id, arguments, ai_config_id):
        return {"result": {"success": True}}

    def fail_bridge(awaitable, timeout=None):
        awaitable.close()
        raise TimeoutError("bridge deadline")

    monkeypatch.setattr(tool_execution, "call_mcp_or_endpoint_tool", fake_call)
    monkeypatch.setattr(tool_execution, "is_endpoint_agent_tool", lambda _tool: False)
    monkeypatch.setattr(tool_execution, "run_async", fail_bridge)

    execution = tool_execution.execute_tool_call("workspace.read", 9, {}, None)

    assert execution.failed is True
    assert "bridge deadline" in execution.error
    assert execution.result == {
        "result": {"success": False, "error": execution.error}
    }


def test_joined_tool_events_preserve_skip_and_execution_order(monkeypatch):
    waiting = []

    def fake_skip_reason(tool, arguments, allowed_tools):
        return "unsafe joined call" if tool == "dangerous" else ""

    def fake_execute(tool, user_id, arguments, ai_config_id):
        return tool_execution.ToolExecutionResult(
            result={"result": {"success": True, "tool": tool}},
            failed=False,
            error="",
            display_text=tool,
            latency=0.25,
        )

    monkeypatch.setattr(tool_execution, "joined_tool_skip_reason", fake_skip_reason)
    monkeypatch.setattr(tool_execution, "execute_tool_call", fake_execute)
    request = tool_execution.JoinedToolRequest(
        tools=("dangerous", "workspace.read"),
        arguments={"path": "README.md"},
        allowed_tools=frozenset({"dangerous", "workspace.read"}),
        user_id=9,
        ai_config_id=3,
    )

    events = list(
        tool_execution.iter_joined_tool_executions(
            request,
            should_stop=lambda: False,
            mark_waiting=lambda tool, arguments: waiting.append((tool, arguments)),
        )
    )

    assert [event.tool for event in events] == ["dangerous", "workspace.read"]
    assert events[0].execution.failed is True
    assert events[0].execution.error == "unsafe joined call"
    assert events[1].execution.failed is False
    assert waiting == [("workspace.read", {"path": "README.md"})]


def test_joined_tool_events_emit_explicit_stop_before_next_call():
    request = tool_execution.JoinedToolRequest(
        tools=("workspace.read",),
        arguments={},
        allowed_tools=frozenset({"workspace.read"}),
        user_id=9,
        ai_config_id=None,
    )

    events = list(
        tool_execution.iter_joined_tool_executions(
            request,
            should_stop=lambda: True,
            mark_waiting=lambda _tool, _arguments: None,
        )
    )

    assert len(events) == 1
    assert events[0].stopped is True
    assert events[0].execution is None
