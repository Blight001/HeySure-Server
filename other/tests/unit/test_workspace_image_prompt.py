from mcp_runtime.mcp.builtin_catalog import BUILTIN_TOOLS


def test_workspace_file_catalog_explicitly_routes_image_viewing_away_from_console():
    tool = next(item for item in BUILTIN_TOOLS if item.name == "workspace.file+manage")

    assert "action=view_image" in tool.description
    assert "控制台列出路径不会" in tool.description
    assert "file_ref" in tool.description
    assert "workspace_path" in tool.description
