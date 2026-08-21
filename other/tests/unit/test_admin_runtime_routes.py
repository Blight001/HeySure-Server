from types import SimpleNamespace

import pytest
from fastapi import HTTPException

from api.services import repo_update, repo_versions
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


def test_service_mutation_guard_rejects_active_repo_operation(monkeypatch):
    synced = []
    monkeypatch.setattr(repo_versions, "sync_remote_rollback_state", lambda: synced.append("rollback"))
    monkeypatch.setattr(repo_update, "sync_remote_update_state", lambda: synced.append("update"))
    monkeypatch.setattr(
        repo_update,
        "get_state",
        lambda: {"running": True, "phase": "backing_up"},
    )

    with pytest.raises(HTTPException) as captured:
        admin_runtime_routes._ensure_repo_operation_idle()

    assert synced == ["rollback", "update"]
    assert captured.value.status_code == 409
    assert "backing_up" in str(captured.value.detail)


def test_service_mutation_guard_allows_idle_state(monkeypatch):
    monkeypatch.setattr(repo_versions, "sync_remote_rollback_state", lambda: None)
    monkeypatch.setattr(repo_update, "sync_remote_update_state", lambda: None)
    monkeypatch.setattr(repo_update, "get_state", lambda: {"running": False, "phase": "idle"})

    admin_runtime_routes._ensure_repo_operation_idle()


def test_remote_update_state_is_adopted_after_gateway_restart(monkeypatch):
    original = repo_update.get_state()
    monkeypatch.setattr(repo_update.settings, "repo_updater_url", "http://host-updater")
    monkeypatch.setattr(
        repo_update,
        "_remote_request",
        lambda *_args, **_kwargs: {
            "state": {
                "running": True,
                "phase": "backing_up",
                "operation": "update",
                "operation_id": "0123456789abcdef",
                "message": "backup",
                "logs": ["started"],
            }
        },
    )
    try:
        repo_update._set_state(phase="idle", running=False, trigger="", steps=repo_update._fresh_steps())

        repo_update.sync_remote_update_state()

        adopted = repo_update.get_state()
        assert adopted["running"] is True
        assert adopted["phase"] == "backing_up"
        assert adopted["trigger"] == "update"
        assert adopted["operation_id"] == "0123456789abcdef"
        assert adopted["steps"][1]["status"] == "active"
    finally:
        with repo_update._state_lock:
            repo_update._state.clear()
            repo_update._state.update(original)
