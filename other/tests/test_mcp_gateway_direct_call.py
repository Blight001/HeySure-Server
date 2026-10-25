import asyncio
import inspect
from types import SimpleNamespace

import pytest

from gateway.routers import mcp
from tools.console import CONSOLE_EXECUTE_SCHEMA, _console_execute, console_device_request


def test_direct_call_keeps_endpoint_classifier_as_module_dependency():
    """A conditional local import used to shadow the module-level helper.

    Calls without ``ai_config_id`` then raised ``UnboundLocalError`` before
    reaching MCP Runtime.  Lock down the shape because this route is also the
    console/admin test path for library governance tools.
    """
    source = inspect.getsource(mcp.call_mcp_tool)
    assert "from connector_runtime.dispatch.desktop_device_tools import is_endpoint_agent_tool" not in source
    assert callable(mcp.is_endpoint_agent_tool)


def test_endpoint_console_call_requires_ai_config_scope(monkeypatch):
    monkeypatch.setattr(mcp, "is_endpoint_agent_tool", lambda name: name == "run_command")

    with pytest.raises(mcp.HTTPException) as exc_info:
        mcp._require_endpoint_ai_config("run_command", None)

    assert exc_info.value.status_code == 400
    assert "ai_config_id" in str(exc_info.value.detail)


def test_server_tool_can_keep_unscoped_console_path(monkeypatch):
    monkeypatch.setattr(mcp, "is_endpoint_agent_tool", lambda _name: False)

    # Server-owned tools remain available to the existing unscoped library /
    # admin test flow; only live endpoint routing requires an AI context.
    mcp._require_endpoint_ai_config("workspace.run+command", None)


def test_console_contract_is_schema_first_and_workspace_scoped(monkeypatch):
    observed = {}

    def fake_run(user_id, args, ai_config_id):
        observed["call"] = (user_id, args, ai_config_id)
        return {"success": True}

    monkeypatch.setattr("tools.workspace._run_command", fake_run)
    result = _console_execute(7, {"target": "workspace", "command": "echo ok"}, 42)

    assert result == {"success": True}
    assert observed["call"] == (7, {"command": "echo ok"}, 42)
    assert CONSOLE_EXECUTE_SCHEMA["properties"]["target"]["enum"] == ["workspace", "device"]
    assert console_device_request({"target": "device", "tool": "desktop_screenshot", "arguments": {"action": "full"}}) == (
        "desktop_screenshot", {"action": "full"}
    )


def test_internal_console_handler_rejects_unrouted_device_target():
    with pytest.raises(mcp.HTTPException) as exc_info:
        _console_execute(7, {"target": "device", "command": "dir"}, 42)

    assert exc_info.value.status_code == 400
    assert "endpoint MCP" in str(exc_info.value.detail)


def test_job_payload_does_not_leak_arguments_and_decodes_result():
    class Row:
        task_id = "atask_1"
        status = "completed"
        success = True
        summary = "done"
        error = None
        result_json = '{"stdout":"ok"}'
        tool = "run_command"
        device_id = "desktop-1"
        ai_config_id = 42
        created_at = 1.0
        completed_at = 2.0

    payload = mcp._dispatch_job_payload(Row())
    assert payload["result"] == {"stdout": "ok"}
    assert "args" not in payload


def test_endpoint_job_route_returns_persisted_task(monkeypatch):
    cfg = SimpleNamespace(id=42, user_id=7, mcp_enabled=True)
    row = SimpleNamespace(
        task_id="atask_1", status="pending", success=None, summary=None,
        error=None, result_json=None, tool="run_command", device_id="desktop-1",
        ai_config_id=42, created_at=1.0, completed_at=None,
    )

    class Result:
        def __init__(self, value):
            self.value = value

        def first(self):
            return self.value

    class Session:
        def __init__(self):
            self.calls = 0

        def exec(self, _statement):
            self.calls += 1
            return Result(cfg if self.calls == 1 else row)

        def expire_all(self):
            return None

    async def fake_dispatch_endpoint_tool(**kwargs):
        assert kwargs["tool"] == "run_command"
        return "atask_1"

    monkeypatch.setattr(mcp, "get_current_user", lambda _auth, _session: SimpleNamespace(id=7))
    monkeypatch.setattr(mcp, "is_endpoint_agent_tool", lambda _tool: True)
    monkeypatch.setattr(
        "api.services.mcp.capability_view.ensure_tool_eligible",
        lambda *_args, **_kwargs: None,
    )
    monkeypatch.setattr(
        "connector_runtime.dispatch.device_dispatch.dispatch_endpoint_tool",
        fake_dispatch_endpoint_tool,
    )

    payload = asyncio.run(
        mcp.create_mcp_job(
            mcp.MCPJobCreateRequest(
                tool="run_command",
                ai_config_id=42,
                arguments={"command": "echo ok"},
            ),
            Session(),
            "Bearer test",
        )
    )

    assert payload["job_id"] == "atask_1"
    assert payload["status"] == "pending"


def test_tool_catalog_initializes_optional_config_before_response():
    source = inspect.getsource(mcp.list_mcp_tools)
    assert "cfg = None" in source


def test_inheritance_test_contract_carries_ai_scope():
    fields = getattr(mcp.InheritanceMcpTestRequest, "model_fields", None)
    if fields is None:
        fields = mcp.InheritanceMcpTestRequest.__fields__
    assert "ai_config_id" in fields
    field = fields["ai_config_id"]
    required = field.is_required() if hasattr(field, "is_required") else field.required
    assert required is False
