from mcp_runtime.mcp.builtin_catalog import BUILTIN_TOOLS


def test_workspace_file_catalog_explicitly_routes_image_viewing_away_from_console():
    tool = next(item for item in BUILTIN_TOOLS if item.name == "workspace.file+manage")

    assert "action=view_image" in tool.description
    assert "控制台列出路径不会" in tool.description
    assert "file_ref" in tool.description
    assert "workspace_path" in tool.description
    assert "只管理 HeySure 服务器上当前 AI 工作区里的文件" in tool.description
    assert "端侧文件必须先通过对应设备工具上传/同步" in tool.description
    assert "只有服务器 AI 工作区文件" in tool.description


def test_workspace_command_catalog_routes_host_changes_to_baota():
    tool = next(item for item in BUILTIN_TOOLS if item.name == "workspace.run+command")

    assert "不是用户电脑、浏览器插件、桌面端设备或宝塔主机的 shell" in tool.description
    assert "必须使用 baota MCP" in tool.description
    assert "本机、桌面、浏览器和下载目录必须使用对应端侧设备 MCP" in tool.description
    assert "服务器主机路径请改用 baota MCP" in tool.input_schema["properties"]["cwd"]["description"]


def test_message_catalog_only_accepts_direct_files_from_server_ai_workspace():
    tool = next(item for item in BUILTIN_TOOLS if item.name == "message.send+to")

    assert "只能直接发送 HeySure 服务器当前 AI 工作区里的文件" in tool.description
    assert "必须先通过对应设备工具上传/同步" in tool.description
    media_path = tool.input_schema["properties"]["media_path"]["description"]
    assert "仅允许 HeySure 服务器当前 AI 工作区内的文件路径" in media_path
    assert "不能传用户电脑、端侧设备或宝塔主机路径" in media_path
