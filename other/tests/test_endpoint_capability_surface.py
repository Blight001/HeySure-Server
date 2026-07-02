from api.devices.presence import mcp_capabilities
from api.services.device_tools.device_browser_runtime_tools import load_default_tools


def test_browser_defaults_do_not_generate_generic_dispatcher():
    names = {tool["name"] for tool in load_default_tools()}

    assert "browser.run" not in names
    assert "browser_action" in names


def test_remote_control_capability_is_not_an_mcp_tool():
    caps = {"browser_action", "remote_control", "remote.control"}

    assert mcp_capabilities(caps) == {"browser_action"}
