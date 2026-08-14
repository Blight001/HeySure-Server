import json

from api.devices.catalog import prepare_device_catalog
from api.models import DevicePresence
from api.services.mcp.capability_diagnostics import (
    inspect_described_cache,
    inspect_exposed_tools,
    inspect_online_device_catalogs,
    inspect_scoped_tool_view,
)
from api.services.mcp.capability_revision import capability_revision, schema_revision
from api.services.mcp.capability_types import ScopedToolView, ToolCapability


def _view(*, revision=None):
    schema = {"type": "object", "properties": {}}
    capability = ToolCapability(
        canonical_name="workspace.read",
        input_schema=schema,
        schema_version=schema_revision("workspace.read", "read", schema, False),
    )
    eligible = {"workspace.read": capability}
    calculated = capability_revision(eligible, ())
    return ScopedToolView(
        revision=calculated if revision is None else revision,
        eligible=eligible,
    )


def _online_presence():
    payload = {
        "capabilities": ["browser.publish"],
        "toolDefs": [{
            "name": "browser.publish",
            "description": "publish",
            "input_schema": {"type": "object", "properties": {}},
        }],
        "aiDescription": "用于发布内容",
        "catalogProtocolVersion": 2,
    }
    prepared = prepare_device_catalog(payload)
    return DevicePresence(
        user_id=1,
        device_id="browser-1",
        device_type="browser",
        capabilities_json=json.dumps(list(prepared.capabilities)),
        tool_defs_json=json.dumps(prepared.tool_defs_map),
        reported_ai_description=prepared.reported_ai_description,
        catalog_generation=1,
        catalog_hash=prepared.catalog_hash,
        catalog_protocol_version=prepared.protocol_version,
        online=True,
    )


def test_scoped_view_revision_is_deterministic_and_surface_is_consistent():
    assert inspect_scoped_tool_view(_view())["ok"] is True
    mismatch = inspect_scoped_tool_view(_view(revision="wrong"))
    assert mismatch["ok"] is False
    assert "revision" in mismatch["problems"][0]


def test_exposed_tools_must_be_subset_of_eligible():
    healthy = inspect_exposed_tools(_view(), {"workspace.read"})
    broken = inspect_exposed_tools(_view(), {"workspace.read", "secret.tool"})
    assert healthy == {"ok": True, "exposed_count": 1, "ineligible_exposed_count": 0}
    assert broken["ok"] is False
    assert broken["ineligible_exposed_count"] == 1


def test_described_cache_separates_restorable_stale_and_malformed_entries():
    view = _view()
    version = view.eligible["workspace.read"].schema_version
    report = inspect_described_cache(view, {
        "workspace.read": {"schema_version": version},
        "removed.tool": {"schema_version": "old"},
        "broken.tool": {},
        "wrong-shape.tool": "not-an-object",
    })
    assert report["ok"] is False
    assert report["restorable_count"] == 1
    assert report["stale_count"] == 1
    assert report["malformed_count"] == 2
    assert report["ineligible_exposed_count"] == 0


def test_online_catalog_hash_and_generation_are_recomputed():
    valid = _online_presence()
    legacy_offline = DevicePresence(
        user_id=1,
        device_id="old-offline",
        online=False,
        catalog_generation=0,
        catalog_hash="",
    )
    healthy = inspect_online_device_catalogs([valid, legacy_offline])
    assert healthy["ok"] is True
    assert healthy["online_count"] == 1

    valid.catalog_hash = "0" * 64
    broken = inspect_online_device_catalogs([valid])
    assert broken["ok"] is False
    assert broken["invalid"][0]["reasons"] == ["hash_mismatch"]
