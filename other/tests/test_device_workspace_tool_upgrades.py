from api.services.device_tools import device_workspace_tools as workspace_tools
from api.services.device_tools import device_dynamic_tools
from api.services.device_tools import device_runtime_tools


def _legacy_python_tool(name: str = "clipboard.get", source: str = "result = 'legacy'\n"):
    return device_dynamic_tools.validate_definition({
        "name": name,
        "description": "legacy factory tool",
        "input_schema": {"type": "object"},
        "code_kind": "runtime",
        "runtime": "python",
        "source": source,
        "permissions": [],
    })


def test_seed_defaults_upgrades_exact_legacy_python_tool(tmp_path, monkeypatch):
    tools_dir = tmp_path / "desktop"
    monkeypatch.setattr(workspace_tools, "_tools_dir", lambda _user_id, _dtype: str(tools_dir))

    legacy = _legacy_python_tool()
    workspace_tools._write_files(str(tools_dir), legacy, enabled=False, status="archived")
    legacy_revision = device_dynamic_tools._revision(workspace_tools._definition_of(legacy))
    monkeypatch.setitem(
        device_runtime_tools.LEGACY_PYTHON_DEFAULT_REVISIONS,
        legacy["name"],
        legacy_revision,
    )

    created = workspace_tools.seed_defaults(1, "desktop")
    upgraded = workspace_tools.get_tool(1, "desktop", legacy["name"])

    assert created >= 1
    assert upgraded is not None
    assert upgraded["runtime"] == "powershell"
    assert upgraded["enabled"] is False
    assert upgraded["status"] == "archived"
    assert not (tools_dir / f"{legacy['name']}.py").exists()
    assert (tools_dir / f"{legacy['name']}.ps1").is_file()


def test_seed_defaults_preserves_user_edited_python_tool(tmp_path, monkeypatch):
    tools_dir = tmp_path / "desktop"
    monkeypatch.setattr(workspace_tools, "_tools_dir", lambda _user_id, _dtype: str(tools_dir))

    factory = _legacy_python_tool()
    edited = _legacy_python_tool(source="result = 'user edit'\n")
    workspace_tools._write_files(str(tools_dir), edited, enabled=True, status="active")
    factory_revision = device_dynamic_tools._revision(workspace_tools._definition_of(factory))
    monkeypatch.setitem(
        device_runtime_tools.LEGACY_PYTHON_DEFAULT_REVISIONS,
        factory["name"],
        factory_revision,
    )

    workspace_tools.seed_defaults(1, "desktop")
    preserved = workspace_tools.get_tool(1, "desktop", edited["name"])

    assert preserved is not None
    assert preserved["runtime"] == "python"
    assert preserved["source"] == edited["source"]
    assert (tools_dir / f"{edited['name']}.py").is_file()
    assert not (tools_dir / f"{edited['name']}.ps1").exists()
