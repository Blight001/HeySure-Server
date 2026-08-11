from sqlalchemy.pool import StaticPool
from sqlmodel import create_engine

from api.devices import bindings, live, mcp_permissions
from api.models import DeviceAiBinding, DeviceTypeMcpPermission
from connector_runtime.dispatch import desktop_device_tools


def _memory_engine():
    db = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    DeviceAiBinding.__table__.create(db)
    DeviceTypeMcpPermission.__table__.create(db)
    return db


def test_physical_device_members_can_be_added_and_removed_independently(monkeypatch):
    db = _memory_engine()
    monkeypatch.setattr(bindings, "engine", db)

    assert bindings.set_member_binding(1, "phone-1", 11, bound=True) == [11]
    assert bindings.set_member_binding(1, "phone-1", 12, bound=True) == [11, 12]
    assert bindings.get_binding(1, "phone-1") == 11

    assert bindings.set_member_binding(1, "phone-1", 11, bound=False) == [12]
    assert bindings.get_bindings(1, "phone-1") == [12]


def test_mcp_scopes_are_isolated_per_bound_member(monkeypatch):
    db = _memory_engine()
    monkeypatch.setattr(mcp_permissions, "engine", db)

    mcp_permissions.set_scope(1, "phone-1", {"screen.read", "touch.tap"})
    mcp_permissions.set_scope(1, "phone-1", {"screen.read"}, ai_config_id=11)
    mcp_permissions.set_scope(1, "phone-1", {"touch.tap"}, ai_config_id=12)

    assert mcp_permissions.get_scope(1, "phone-1", 11) == {"screen.read"}
    assert mcp_permissions.get_scope(1, "phone-1", 12) == {"touch.tap"}
    assert mcp_permissions.get_scope(1, "phone-1", 13) == {"screen.read", "touch.tap"}
    assert mcp_permissions.get_scope(1, "phone-1") == {"screen.read", "touch.tap"}


def test_dispatch_selects_only_a_device_that_allows_the_member_tool(monkeypatch):
    db = _memory_engine()
    monkeypatch.setattr(bindings, "engine", db)
    monkeypatch.setattr(mcp_permissions, "engine", db)
    bindings.set_member_binding(1, "phone-denied", 11, bound=True)
    bindings.set_member_binding(1, "phone-allowed", 11, bound=True)
    mcp_permissions.set_scope(1, "phone-denied", {"screen.read"}, ai_config_id=11)
    mcp_permissions.set_scope(1, "phone-allowed", {"touch.tap"}, ai_config_id=11)
    monkeypatch.setattr(
        desktop_device_tools,
        "agents",
        {
            "sid-a": {
                "id": "phone-denied",
                "userId": 1,
                "deviceType": "android",
                "capabilities": ["screen.read", "touch.tap"],
            },
            "sid-b": {
                "id": "phone-allowed",
                "userId": 1,
                "deviceType": "android",
                "capabilities": ["touch.tap"],
            },
        },
    )

    selected = desktop_device_tools.get_connected_desktop_agent(11, 1, tool="touch.tap")

    assert selected and selected["id"] == "phone-allowed"


def test_endpoint_binding_overlay_preserves_builtin_toolbox_members(monkeypatch):
    monkeypatch.setattr(
        bindings,
        "bindings_by_device_for_user",
        lambda _user_id: {"phone-1": [12]},
    )
    rows = [
        {
            "id": "toolbox_builtin_1",
            "deviceType": "toolbox",
            "isToolbox": True,
            "boundAiConfigIds": [11, 12],
        },
        {"id": "phone-1", "deviceType": "android", "boundAiConfigIds": []},
    ]

    live._apply_endpoint_bindings(rows, 1)

    assert rows[0]["boundAiConfigIds"] == [11, 12]
    assert rows[1]["boundAiConfigIds"] == [12]
