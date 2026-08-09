from gateway.routers import admin_services


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
    assert admin_services.probe_service(target)["status"] == "running"


def test_remote_probe_and_logs_close_internal_clients(monkeypatch):
    _FakeClient.instances.clear()
    monkeypatch.setattr(admin_services, "InternalClient", _FakeClient)
    target = admin_services.ServiceTarget("ai", "AI 运行时", "http://ai:3003")

    status = admin_services.probe_service(target)
    logs = admin_services.fetch_service_logs(target, limit=12, level="ERROR")

    assert status["status"] == "running"
    assert logs["lines"] == ["ready"]
    assert all(client.closed for client in _FakeClient.instances)


def test_restart_remote_service_returns_runtime_payload(monkeypatch):
    _FakeClient.instances.clear()
    monkeypatch.setattr(admin_services, "InternalClient", _FakeClient)
    target = admin_services.ServiceTarget("mcp", "MCP 运行时", "http://mcp:3001")

    result = admin_services.restart_remote_service(target)

    assert result == {"restarting": True}
    assert _FakeClient.instances[-1].closed is True
