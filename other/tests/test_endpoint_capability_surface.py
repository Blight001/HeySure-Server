from api.devices.presence import mcp_capabilities
from api.services.device_tools.device_browser_runtime_tools import load_default_tools
from api.services.device_tools.device_runtime_tools import load_default_tools as load_desktop_tools


def test_browser_defaults_do_not_generate_generic_dispatcher():
    names = {tool["name"] for tool in load_default_tools()}

    assert "browser.run" not in names
    assert "browser_action" in names


def test_desktop_has_no_factory_catalog():
    assert load_desktop_tools() == []


def test_remote_control_capability_is_not_an_mcp_tool():
    caps = {"browser_action", "remote_control", "remote.control"}

    assert mcp_capabilities(caps) == {"browser_action"}
