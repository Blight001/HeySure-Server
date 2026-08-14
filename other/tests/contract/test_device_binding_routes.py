from gateway.routers.workshop import router
from gateway.routers.devices import router as endpoint_router


def test_device_binding_routes_are_canonical_and_legacy_routes_are_hidden():
    routes = {route.path: route for route in router.routes}
    assert "/devices/builtin-bindings" in routes
    assert "/devices/library-mcp-scope" not in routes
    assert routes["/devices/builtin-bindings"].include_in_schema is True
    assert routes["/workshop/bindings"].include_in_schema is False
    assert "/workshop/mcp-scope" not in routes


def test_endpoint_multi_member_binding_route_is_public():
    routes = {route.path: route for route in endpoint_router.routes}
    assert "/{device_id}/member-bindings/{ai_config_id}" in routes
    assert routes["/{device_id}/member-bindings/{ai_config_id}"].methods == {"PUT"}
    assert "/{device_id}/mcp-scope" in routes
