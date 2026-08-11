from types import SimpleNamespace

from gateway.routers import admin_runtime_routes


def test_admin_runtime_router_exposes_expected_paths():
    paths = {route.path for route in admin_runtime_routes.router.routes}
    assert paths == {
        "/services",
        "/services/rebuild-all",
        "/services/restart-all",
        "/services/{key}/logs",
        "/services/{key}/restart",
        "/tasks",
        "/tasks/{run_id}/stop",
    }
    assert admin_runtime_routes.PREFIX == "/api/admin"


def test_serialize_task_preserves_admin_contract():
    run = SimpleNamespace(
        run_id="run-1",
        status="running",
        stop_requested=False,
        user_id=7,
        ai_config_id=8,
        ai_kind="assistant",
        session_id="session-1",
        session_name="Session",
        error_message=None,
        started_at=1.0,
        finished_at=None,
        heartbeat_at=2.0,
        created_at=0.0,
        updated_at=3.0,
    )
    owner = SimpleNamespace(name="Owner", account="owner")

    payload = admin_runtime_routes._serialize_task(run, owner)

    assert payload["run_id"] == "run-1"
    assert payload["user_name"] == "Owner"
    assert payload["user_account"] == "owner"
    assert payload["updated_at"] == 3.0
