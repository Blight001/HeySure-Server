from types import SimpleNamespace

from gateway.routers import admin_audit_routes


def test_admin_audit_router_exposes_audit_path():
    assert {route.path for route in admin_audit_routes.router.routes} == {"/audit"}


def test_serialize_audit_entry_preserves_contract():
    row = SimpleNamespace(
        id=1,
        created_at=2.0,
        actor_id=3,
        actor_account="owner",
        action="restart_service",
        target_type="service",
        target_id="ai",
        target_label="AI 运行时",
        detail="restart",
    )
    assert admin_audit_routes.serialize_audit_entry(row) == {
        "id": 1,
        "created_at": 2.0,
        "actor_id": 3,
        "actor_account": "owner",
        "action": "restart_service",
        "target_type": "service",
        "target_id": "ai",
        "target_label": "AI 运行时",
        "detail": "restart",
    }
