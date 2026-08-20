from __future__ import annotations

import json
from types import SimpleNamespace

from other.scripts import host_rescue


def test_parse_compose_rows_accepts_array_and_json_lines() -> None:
    rows = [{"Service": "api-gateway", "State": "running"}]
    assert host_rescue._parse_compose_rows(json.dumps(rows)) == rows
    assert host_rescue._parse_compose_rows("\n".join(json.dumps(row) for row in rows)) == rows


def test_service_statuses_are_allowlisted_and_do_not_expose_compose_payload(monkeypatch) -> None:
    raw = json.dumps([{
        "Service": "api-gateway",
        "State": "running",
        "Health": "healthy",
        "Status": "Up 2 minutes",
        "Labels": "secret=value",
    }])
    monkeypatch.setattr(host_rescue, "_run_compose", lambda *args, **kwargs: raw)

    statuses = host_rescue.service_statuses()

    assert [item["service"] for item in statuses] == list(host_rescue.SERVICES)
    assert statuses[0] == {
        "service": "api-gateway",
        "state": "running",
        "health": "healthy",
        "status": "Up 2 minutes",
    }
    assert "secret" not in json.dumps(statuses)


def test_public_outage_signal_requires_every_runtime_to_be_unavailable() -> None:
    unavailable = [
        {"service": service, "state": "exited", "health": "", "status": "stopped"}
        for service in host_rescue.SERVICES
    ]
    assert host_rescue.all_runtimes_unavailable(unavailable) is True

    unhealthy = [
        {"service": service, "state": "running", "health": "unhealthy", "status": "Up (unhealthy)"}
        for service in host_rescue.SERVICES
    ]
    assert host_rescue.all_runtimes_unavailable(unhealthy) is True

    unavailable[0] = {
        "service": "api-gateway",
        "state": "running",
        "health": "starting",
        "status": "Up 2 seconds",
    }
    assert host_rescue.all_runtimes_unavailable(unavailable) is False


def test_public_health_fails_closed_when_compose_status_is_unknown(monkeypatch) -> None:
    def fail() -> list[dict[str, str]]:
        raise host_rescue.RescueError("compose status unavailable")

    monkeypatch.setattr(host_rescue, "service_statuses", fail)
    payload = host_rescue.public_health_status()

    assert payload["ok"] is True
    assert payload["all_runtimes_unavailable"] is False
    assert "services" not in payload


def test_queue_recovery_rejects_arbitrary_service_names() -> None:
    assert host_rescue.queue_recovery("db") == {
        "ok": False,
        "error": "unsupported recovery action",
    }


def test_recover_worker_uses_fixed_compose_arguments(monkeypatch) -> None:
    calls = []
    monkeypatch.setattr(host_rescue, "_run_compose", lambda *args, **kwargs: calls.append(args) or "")
    assert host_rescue._action_lock.acquire(blocking=False)

    host_rescue._recover_worker("restart_gateway", automatic=False)

    assert calls == [("up", "-d", "--no-deps", "--force-recreate", "api-gateway")]


def test_origin_policy_allows_same_host_or_explicit_origin(monkeypatch) -> None:
    monkeypatch.setattr(host_rescue, "ALLOWED_ORIGINS", {"https://console.example.com"})
    assert host_rescue._origin_allowed("http://49.234.181.190:58150", "49.234.181.190:58152")
    assert host_rescue._origin_allowed("https://console.example.com", "rescue.internal:58152")
    assert not host_rescue._origin_allowed("https://attacker.example", "rescue.internal:58152")


def test_authorization_fails_closed_without_token(monkeypatch) -> None:
    handler = SimpleNamespace(headers={"Authorization": "Bearer anything"})
    monkeypatch.setattr(host_rescue, "TOKEN", "")

    assert host_rescue.Handler._authorized(handler) is False
