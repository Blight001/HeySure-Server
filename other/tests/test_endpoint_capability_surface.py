from types import SimpleNamespace

from api.devices.presence import _offline_device_projection, mcp_capabilities
from api.services.device_tools.device_browser_runtime_tools import load_default_tools
from api.services.device_tools.device_runtime_tools import load_default_tools as load_desktop_tools


def test_browser_defaults_do_not_generate_generic_dispatcher():
    names = {tool["name"] for tool in load_default_tools()}

    assert "browser.run" not in names
    assert "browser_action" in names


def test_desktop_defaults_are_action_grouped():
    names = {tool["name"] for tool in load_desktop_tools()}

    assert names == {
        "run_command",
        "desktop_observe",
        "desktop_screenshot",
        "desktop_action",
        "clipboard",
    }
    assert "mouse.click" not in names


def test_remote_control_capability_is_not_an_mcp_tool():
    caps = {
        "browser_action",
        "remote_control",
        "remote.control",
        "remote_web_mirror",
        "remote.web_mirror",
        "remote_controller_templates",
    }

    assert mcp_capabilities(caps) == {"browser_action"}


def test_device_inventory_projection_keeps_remote_transport_capabilities():
    row = SimpleNamespace(
        ai_config_id=None,
        capabilities_json='["arm.move", "remote_control", "remote_controller_templates"]',
        icon="",
        icon_override="",
        name="AI mechanical arm",
        online=True,
        platform="custom-service",
        remark="",
    )

    projected = _offline_device_projection(row, "jibotarm-01", "custom")

    assert projected["capabilities"] == [
        "arm.move",
        "remote_control",
        "remote_controller_templates",
    ]
