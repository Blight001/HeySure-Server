from connector_runtime.app import create_app


def test_connector_runtime_exposes_dispatch_expire_route():
    socket_app = create_app()
    fastapi_app = socket_app.other_asgi_app
    routes = {
        (route.path, method)
        for route in fastapi_app.routes
        for method in (getattr(route, "methods", None) or set())
    }

    assert ("/internal/agent/dispatch/expire/{task_id}", "POST") in routes
