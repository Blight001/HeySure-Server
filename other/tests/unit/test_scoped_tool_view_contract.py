from types import SimpleNamespace

from api.services.mcp.capability_view import ToolViewRequest, resolve_scoped_tool_view


class _Session:
    class _Rows:
        @staticmethod
        def all():
            return []

    def exec(self, *_args, **_kwargs):
        return self._Rows()


def _patch_sources(monkeypatch):
    from connector_runtime.dispatch import desktop_device_tools
    from api.devices import bindings, presence, workshop_bindings
    from mcp_runtime.mcp import registry

    monkeypatch.setattr(registry, "list_tools", lambda: [
        {
            "name": "mcp.describe+tool",
            "description": "describe",
            "inputSchema": {},
            "destructive": False,
        },
        {
            "name": "workspace.search",
            "description": "search",
            "inputSchema": {"type": "object"},
            "destructive": False,
        },
        {
            "name": "member.manage",
            "description": "manage",
            "inputSchema": {},
            "destructive": True,
        },
    ])
    monkeypatch.setattr(presence, "online_tool_catalog_for_user", lambda _uid: [{
        "device_id": "browser-1",
        "device_type": "browser",
        "tools": [{
            "name": "browser.publish",
            "description": "publish",
            "input_schema": {"type": "object"},
            "destructive": True,
        }],
    }])
    monkeypatch.setattr(
        desktop_device_tools,
        "endpoint_tools_for_config",
        lambda *_args: {"browser.publish"},
    )
    monkeypatch.setattr(desktop_device_tools, "endpoint_bridge_tools_for_config", lambda *_args: set())
    monkeypatch.setattr(desktop_device_tools, "toolbox_tools_for_config", lambda *_args: {"workspace.search"})
    monkeypatch.setattr(bindings, "device_ids_for_config", lambda *_args: {"browser-1"})
    monkeypatch.setattr(workshop_bindings, "config_bound_to_library", lambda *_args: False)


def _resolve(monkeypatch, *, selected=None):
    _patch_sources(monkeypatch)
    user = SimpleNamespace(id=7)
    cfg = SimpleNamespace(
        id=9,
        user_id=7,
        mcp_enabled=True,
        mcp_tools='["member.manage"]',
        ai_role="digital_member",
        digital_member_role="member",
    )
    return resolve_scoped_tool_view(
        _Session(),
        user,
        cfg,
        ToolViewRequest(
            ai_config_id=9,
            selected_tools=frozenset(selected) if selected is not None else None,
        ),
    )


def test_scoped_view_unifies_server_device_and_binding_eligibility(monkeypatch):
    view = _resolve(monkeypatch)

    assert view.eligible_names == frozenset({
        "mcp.describe+tool",
        "workspace.search",
        "browser.publish",
    })
    assert view.eligible["browser.publish"].source_kind == "device"
    assert view.blocked["member.manage"].reason == "not_eligible"
    assert len(view.revision) == 16


def test_selected_scope_only_narrows_and_preserves_introspection(monkeypatch):
    view = _resolve(monkeypatch, selected={"browser.publish"})

    assert view.eligible_names == frozenset({
        "mcp.describe+tool",
        "browser.publish",
    })


def test_task_tool_hints_cannot_expand_device_authorization(monkeypatch):
    _patch_sources(monkeypatch)
    user = SimpleNamespace(id=7)
    cfg = SimpleNamespace(id=9, user_id=7, mcp_enabled=True)

    view = resolve_scoped_tool_view(
        _Session(),
        user,
        cfg,
        ToolViewRequest(
            ai_config_id=9,
            task_required_tools=frozenset({"member.manage"}),
            extra_required_tools=frozenset({"member.manage"}),
            override_tools=frozenset({"member.manage", "workspace.search"}),
        ),
    )

    assert view.eligible_names == frozenset({"mcp.describe+tool", "workspace.search"})


def test_server_automation_is_unavailable_without_toolbox_binding(monkeypatch):
    from api.devices import presence
    from connector_runtime.dispatch import desktop_device_tools
    from mcp_runtime.mcp import registry

    _patch_sources(monkeypatch)
    monkeypatch.setattr(registry, "list_tools", lambda: [{
        "name": "automation.manage", "description": "server workflow", "inputSchema": {},
        "destructive": True,
    }])
    monkeypatch.setattr(presence, "online_tool_catalog_for_user", lambda _uid: [])
    monkeypatch.setattr(desktop_device_tools, "endpoint_tools_for_config", lambda *_args: set())
    monkeypatch.setattr(desktop_device_tools, "toolbox_tools_for_config", lambda *_args: set())
    user = SimpleNamespace(id=7)
    cfg = SimpleNamespace(
        id=9, user_id=7, mcp_enabled=True, mcp_tools="[]",
        ai_role="digital_member", digital_member_role="member",
    )

    view = resolve_scoped_tool_view(_Session(), user, cfg, ToolViewRequest(ai_config_id=9))

    assert view.eligible_names == frozenset()
    assert view.devices == ()


def test_revision_is_stable_for_same_scoped_inputs(monkeypatch):
    first = _resolve(monkeypatch)
    second = _resolve(monkeypatch)

    assert first.revision == second.revision


def test_capability_schema_version_matches_describe_contract(monkeypatch):
    from tools.introspection import _with_schema_version

    view = _resolve(monkeypatch)
    capability = view.eligible["workspace.search"]
    described = _with_schema_version({
        "name": capability.canonical_name,
        "description": capability.description,
        "inputSchema": dict(capability.input_schema),
        "destructive": capability.destructive,
    })

    assert capability.schema_version == described["schemaVersion"]


def test_conflicting_bound_device_schemas_fail_closed(monkeypatch):
    from api.devices import bindings, presence
    from connector_runtime.dispatch import desktop_device_tools

    _patch_sources(monkeypatch)
    monkeypatch.setattr(bindings, "device_ids_for_config", lambda *_args: {"browser-1", "browser-2"})
    monkeypatch.setattr(desktop_device_tools, "endpoint_tools_for_config", lambda *_args: {"browser.publish"})
    monkeypatch.setattr(presence, "online_tool_catalog_for_user", lambda _uid: [
        {
            "device_id": "browser-1",
            "device_type": "browser",
            "tools": [{"name": "browser.publish", "input_schema": {"type": "object"}}],
        },
        {
            "device_id": "browser-2",
            "device_type": "browser",
            "tools": [{
                "name": "browser.publish",
                "input_schema": {"type": "object", "required": ["title"]},
            }],
        },
    ])
    user = SimpleNamespace(id=7)
    cfg = SimpleNamespace(
        id=9, user_id=7, mcp_enabled=True, mcp_tools="[]",
        ai_role="digital_member", digital_member_role="member",
    )

    view = resolve_scoped_tool_view(
        _Session(), user, cfg, ToolViewRequest(ai_config_id=9)
    )

    assert "browser.publish" not in view.eligible_names
    assert view.blocked["browser.publish"].reason == "schema_conflict"
