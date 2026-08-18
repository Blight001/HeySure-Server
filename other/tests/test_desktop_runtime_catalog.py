from api.services.device_tools import device_dynamic_tools
from api.services.device_tools import device_runtime_tools
from api.services.device_tools import device_workspace_tools


def test_desktop_factory_catalog_is_empty():
    assert device_runtime_tools.load_default_tools() == []


def test_seed_defaults_does_not_plant_desktop_factory_tools(tmp_path, monkeypatch):
    tools_dir = tmp_path / "desktop"
    monkeypatch.setattr(device_workspace_tools, "_tools_dir", lambda _user_id, _dtype: str(tools_dir))

    created = device_workspace_tools.seed_defaults(1, "desktop")
    names = {tool["name"] for tool in device_workspace_tools.list_tools(1, "desktop")}

    assert created == 0
    assert names == set()


def test_seed_defaults_tombstones_untouched_powershell_factory(tmp_path, monkeypatch):
    tools_dir = tmp_path / "desktop"
    monkeypatch.setattr(device_workspace_tools, "_tools_dir", lambda _user_id, _dtype: str(tools_dir))

    factory = device_dynamic_tools.validate_definition({
        "name": "mouse.click",
        "description": "legacy factory tool",
        "input_schema": {"type": "object"},
        "code_kind": "runtime",
        "runtime": "powershell",
        "source": " $result = @{ ok = $true }\n",
        "permissions": [],
    })
    device_workspace_tools._write_files(str(tools_dir), factory, enabled=True, status="active")
    revision = device_dynamic_tools._revision(device_workspace_tools._definition_of(factory))
    monkeypatch.setitem(device_runtime_tools.LEGACY_POWERSHELL_DEFAULT_REVISIONS, "mouse.click", revision)

    device_workspace_tools.seed_defaults(1, "desktop")

    assert device_workspace_tools.get_tool(1, "desktop", "mouse.click") is None
    assert (tools_dir / ".deleted" / "mouse.click").is_file()
    assert device_workspace_tools.list_tools(1, "desktop") == []


def test_seed_defaults_preserves_user_edited_retired_tool(tmp_path, monkeypatch):
    tools_dir = tmp_path / "desktop"
    monkeypatch.setattr(device_workspace_tools, "_tools_dir", lambda _user_id, _dtype: str(tools_dir))

    edited = device_dynamic_tools.validate_definition({
        "name": "mouse.click",
        "description": "user customized click",
        "input_schema": {"type": "object"},
        "code_kind": "runtime",
        "runtime": "powershell",
        "source": "$result = @{ custom = $true }\n",
        "permissions": [],
    })
    device_workspace_tools._write_files(str(tools_dir), edited, enabled=True, status="active")

    device_workspace_tools.seed_defaults(1, "desktop")
    preserved = device_workspace_tools.get_tool(1, "desktop", "mouse.click")

    assert preserved is not None
    assert preserved["source"] == edited["source"]
