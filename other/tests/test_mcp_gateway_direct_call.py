import inspect

from gateway.routers import mcp


def test_direct_call_keeps_endpoint_classifier_as_module_dependency():
    """A conditional local import used to shadow the module-level helper.

    Calls without ``ai_config_id`` then raised ``UnboundLocalError`` before
    reaching MCP Runtime.  Lock down the shape because this route is also the
    console/admin test path for library governance tools.
    """
    source = inspect.getsource(mcp.call_mcp_tool)
    assert "from connector_runtime.dispatch.desktop_device_tools import is_endpoint_agent_tool" not in source
    assert callable(mcp.is_endpoint_agent_tool)
