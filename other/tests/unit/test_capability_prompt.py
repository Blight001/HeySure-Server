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
    assert "本组工具运行在所选端侧设备" in device_section
    assert "统一传输流程" in device_section
    assert "browser.publish: 发布内容" in device_section


def test_toolbox_explains_server_device_and_baota_workspace_boundaries(monkeypatch):
    monkeypatch.setattr(
        "mcp_runtime.mcp.permissions.LIBRARY_BOUND_TOOLS",
        set(),
    )
    view = ScopedToolView(
        revision="rev",
        eligible={
            "workspace.run+command": ToolCapability(
                canonical_name="workspace.run+command",
                description="执行命令",
            ),
            "workspace.transfer": ToolCapability(
                canonical_name="workspace.transfer",
                description="文件传输",
            ),
        },
    )

    rendered = render_scoped_tool_catalog(view, user_id=1, ai_config_id=2)

    assert "工具箱 MCP（服务器 AI 工作区）" in rendered
    assert "同名文件或相同相对路径不代表两端文件相同" in rendered
    assert "使用 baota MCP" in rendered
    assert rendered.count("文件传输最小合同") == 1


def test_transfer_contract_is_rendered_once_for_multiple_devices(monkeypatch):
    monkeypatch.setattr(
        "mcp_runtime.mcp.permissions.LIBRARY_BOUND_TOOLS",
        set(),
    )
    devices = tuple(
        DevicePromptMetadata(
            device_id=f"device-{index}",
            name=f"设备 {index}",
            device_type="custom",
            purpose="执行端侧操作",
            tool_count=1,
        )
        for index in (1, 2)
    )
    view = ScopedToolView(
        revision="rev",
        eligible={
            "workspace.transfer": ToolCapability(
                canonical_name="workspace.transfer",
                description="文件传输",
            ),
            "device.action": ToolCapability(
                canonical_name="device.action",
                description="端侧操作",
                source_kind="device",
                device_id="device-1",
            ),
        },
        devices=devices,
        device_tool_names={
            "device-1": frozenset({"device.action"}),
            "device-2": frozenset(),
        },
    )

    rendered = render_scoped_tool_catalog(view, user_id=1, ai_config_id=2)

    assert rendered.count("文件传输最小合同") == 1
    assert "prepare_download" in rendered
    assert "prepare_upload" in rendered
    assert "transport.url" in rendered
