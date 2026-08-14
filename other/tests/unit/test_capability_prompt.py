from api.services.mcp.capability_prompt import render_scoped_tool_catalog
from api.services.mcp.capability_types import (
    DevicePromptMetadata,
    ScopedToolView,
    ToolCapability,
)


def test_device_description_is_rendered_inside_dynamic_mcp_catalog(monkeypatch):
    monkeypatch.setattr(
        "mcp_runtime.mcp.permissions.LIBRARY_BOUND_TOOLS",
        set(),
    )
    view = ScopedToolView(
        revision="rev",
        eligible={
            "browser.publish": ToolCapability(
                canonical_name="browser.publish",
                description="发布内容",
                source_kind="device",
                device_id="browser-1",
            ),
        },
        devices=(DevicePromptMetadata(
            device_id="browser-1",
            name="内容发布台",
            device_type="browser",
            purpose="用于操作已登录的内容后台",
            tool_count=1,
        ),),
        device_tool_names={"browser-1": frozenset({"browser.publish"})},
    )

    rendered = render_scoped_tool_catalog(view, user_id=1, ai_config_id=None)

    device_section = rendered.split("内容发布台 MCP", 1)[1]
    assert "设备说明：用于操作已登录的内容后台" in device_section
    assert "browser.publish: 发布内容" in device_section
