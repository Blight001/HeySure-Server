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
    assert "本组工具运行在该端侧设备" in device_section
    assert "需先上传/同步到服务器 AI 工作区" in device_section
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
        },
    )

    rendered = render_scoped_tool_catalog(view, user_id=1, ai_config_id=2)

    assert "工具箱 MCP（服务器 AI 工作区）" in rendered
    assert "同名文件或相同相对路径也不代表两端文件相同" in rendered
    assert "使用 baota MCP" in rendered
    assert "用户说“AI 工作区/工作区附件/Uploads/Screenshots/file_ref”" in rendered
    assert "用户说“本机/电脑/桌面/浏览器/下载目录/设备端”" in rendered
    assert "只有已经进入服务器 AI 工作区的文件" in rendered
    assert "设备端文件必须先用端侧上传/同步能力复制到服务器工作区" in rendered
