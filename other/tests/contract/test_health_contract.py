from ai_runtime.internal_app import create_status_app
from connector_runtime.app import create_app as create_connector_app
from mcp_runtime.app import create_app as create_mcp_app


REQUIRED_PATHS = {
    "/internal/health/live",
    "/internal/health/ready",
    "/internal/health/detail",
}


def _paths(app):
    return set(app.openapi()["paths"])


def test_leaf_runtimes_expose_standard_health_contract():
    connector = create_connector_app().other_asgi_app
    assert REQUIRED_PATHS <= _paths(connector)
    assert REQUIRED_PATHS <= _paths(create_mcp_app())
    assert REQUIRED_PATHS <= _paths(create_status_app())


def test_compatibility_health_route_is_retained():
    connector = create_connector_app().other_asgi_app
    route_paths = lambda app: {route.path for route in app.routes}
    assert "/internal/health" in route_paths(connector)
    assert "/internal/health" in route_paths(create_mcp_app())
    assert "/internal/health" in route_paths(create_status_app())
