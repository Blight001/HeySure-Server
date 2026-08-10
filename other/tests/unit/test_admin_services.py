from gateway.routers import admin_services
from gateway.routers import admin_service_probes
from gateway.routers import admin_runtime_routes


class _FakeClient:
    instances = []

    def __init__(self, base_url, timeout):
        self.base_url = base_url
        self.timeout = timeout
        self.closed = False
        self.__class__.instances.append(self)

    def get(self, path, params=None):
        if path == "/internal/health":
            return {"ok": True, "role": "worker"}
        return {"lines": ["ready"], "params": params}

    def post(self, path):
        return {"restarting": path == "/internal/restart"}

    def close(self):
        self.closed = True


def test_probe_service_reports_gateway_without_internal_client():
    target = admin_services.ServiceTarget("gateway", "API 网关", "")
    result = admin_services.probe_service(target)
    assert result["status"] in {"running", "degraded"}
    assert result["group"] == "runtime"


def test_remote_probe_and_logs_close_internal_clients(monkeypatch):
    _FakeClient.instances.clear()
    monkeypatch.setattr(admin_services, "InternalClient", _FakeClient)
    target = admin_services.ServiceTarget(
        "ai", "AI 运行时", "http://ai:3003", restartable=True, logs_available=True
    )

    status = admin_services.probe_service(target)
    logs = admin_services.fetch_service_logs(target, limit=12, level="ERROR")

    assert status["status"] == "running"
    assert logs["lines"] == ["ready"]
    assert all(client.closed for client in _FakeClient.instances)


def test_restart_remote_service_returns_runtime_payload(monkeypatch):
    _FakeClient.instances.clear()
    monkeypatch.setattr(admin_services, "InternalClient", _FakeClient)
    target = admin_services.ServiceTarget("mcp", "MCP 运行时", "http://mcp:3001", restartable=True)

    result = admin_services.restart_remote_service(target)

    assert result == {"restarting": True}
    assert _FakeClient.instances[-1].closed is True


def test_service_registry_covers_deployment_and_functional_layers():
    targets = {target.key: target for target in admin_services.service_registry()}

    assert {
        "gateway", "mcp", "connector", "ai", "web", "postgres", "migrations",
        "repo_updater", "agent_socket", "workflow_scheduler", "bot_connections",
    } <= set(targets)
    assert targets["gateway"].restartable is True
    assert targets["postgres"].restartable is False
    assert targets["agent_socket"].group == "channel"


def test_non_log_service_returns_structured_note():
    target = admin_services.ServiceTarget("postgres", "PostgreSQL", "DATABASE_URL", group="infrastructure")

    result = admin_services.fetch_service_logs(target, limit=20, level=None)

    assert result["lines"] == []
    assert "结构化健康信息" in result["note"]


def test_agent_socket_probe_bypasses_environment_proxy(monkeypatch):
    observed = {}

    def fake_http_probe(url, path, **kwargs):
        observed.update({"url": url, "path": path, **kwargs})
        return {
            "status_code": 200,
            "body": {"service_role": "connector", "ready": True},
        }, 1.25

    monkeypatch.setattr(admin_service_probes, "_http_probe", fake_http_probe)

    result = admin_service_probes.probe_agent_socket("http://public.example:3002", required=True)

    assert result["status"] == "running"
    assert observed["path"] == "/internal/health/ready"
    assert observed["trust_env"] is False


def test_list_service_statuses_isolates_probe_failures(monkeypatch):
    targets = [
        admin_services.ServiceTarget("gateway", "API 网关", "(self)"),
        admin_services.ServiceTarget("web", "Web 控制台", "http://web:58150"),
    ]
    monkeypatch.setattr(admin_services, "service_registry", lambda: targets)

    def fake_probe(target):
        if target.key == "web":
            raise RuntimeError("probe failed")
        return admin_services._service_payload(
            target, {"status": "running", "summary": "ok", "detail": {}}
        )

    monkeypatch.setattr(admin_services, "probe_service", fake_probe)

    results = admin_services.list_service_statuses()

    assert [item["key"] for item in results] == ["gateway", "web"]
    assert results[0]["status"] == "running"
    assert results[1]["status"] == "down"
    assert results[1]["detail"] == {"error": "RuntimeError"}


def test_restart_all_restarts_remote_runtimes_then_schedules_gateway(monkeypatch):
    restarted = []
    scheduled = []
    targets = {
        key: admin_services.ServiceTarget(key, key, f"http://{key}:3000", restartable=True)
        for key in ("mcp", "connector", "ai")
    }
    monkeypatch.setattr(admin_runtime_routes, "service_target", targets.get)
    monkeypatch.setattr(admin_runtime_routes, "restart_remote_service", lambda target: restarted.append(target.key))
    monkeypatch.setattr(admin_runtime_routes, "_record_audit", lambda *_args, **_kwargs: None)
    monkeypatch.setattr("api.runtime.process_control.request_restart", lambda delay=0.5: scheduled.append(delay))

    result = admin_runtime_routes.restart_all_services(
        session=object(),
        admin=type("Admin", (), {"account": "owner"})(),
    )

    assert result["ok"] is True
    assert restarted == ["mcp", "connector", "ai"]
    assert result["restarting"] == ["mcp", "connector", "ai", "gateway"]
    assert scheduled == [2.0]
