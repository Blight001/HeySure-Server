from connector_runtime.app import create_app


def test_connector_runtime_exposes_dispatch_expire_route():
    socket_app = create_app()
    fastapi_app = socket_app.other_asgi_app
    operation = fastapi_app.openapi()["paths"][
        "/internal/agent/dispatch/expire/{task_id}"
    ]

    assert "post" in operation
