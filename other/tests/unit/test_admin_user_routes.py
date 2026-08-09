from types import SimpleNamespace

from gateway.routers import admin_user_routes


def test_admin_user_router_exposes_expected_paths():
    paths = {route.path for route in admin_user_routes.router.routes}
    assert paths == {
        "/users",
        "/users/{user_id}",
        "/users/{user_id}/role",
        "/users/{user_id}/reset-password",
    }
    assert admin_user_routes.PREFIX == "/api/admin"


def test_serialize_user_preserves_public_admin_fields():
    user = SimpleNamespace(
        id=5,
        name="Member",
        account="member",
        avatar=None,
        email="member@example.com",
        role="member",
        created_at=123.0,
    )

    payload = admin_user_routes.serialize_user(user)

    assert payload == {
        "id": 5,
        "name": "Member",
        "account": "member",
        "avatar": None,
        "email": "member@example.com",
        "role": "member",
        "role_label": "成员",
        "created_at": 123.0,
    }
