import asyncio

from ai_runtime.inference import core
from ai_runtime.inference.runtime_clients import dispatch_endpoint_via_runtime


def test_split_runtime_preserves_completed_dispatch_device_id(monkeypatch):
    class _Response:
        def __init__(self, payload):
            self.status_code = 200
            self._payload = payload

        def raise_for_status(self):
            return None

        def json(self):
            return self._payload

    class _Client:
        def __init__(self, *args, **kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return None

        async def post(self, path, **kwargs):
            assert path == "/internal/agent/dispatch"
            return _Response({"task_id": "task-a"})

        async def get(self, path, **kwargs):
            assert path == "/internal/agent/dispatch/result/task-a"
            return _Response({
                "status": "completed",
                "success": True,
                "device_id": "linux-a",
                "tool": "shell.run",
                "result": {"stdout": "ok"},
            })

    monkeypatch.setattr("httpx.AsyncClient", _Client)
    monkeypatch.setattr("api.runtime.internal_http.internal_headers", lambda: {})

    result = asyncio.run(dispatch_endpoint_via_runtime(
        "http://connector",
        "shell.run",
        1,
        {"command": "uptime"},
        7,
        poll_interval=0,
    ))

    assert result["taskId"] == "task-a"
    assert result["deviceId"] == "linux-a"


def test_device_identity_uses_completed_dispatch_id_not_shared_tool_name(monkeypatch):
    agents = [
        {
            "id": "linux-b",
            "name": "B 服务器",
            "capabilities": ["shell.run"],
        },
        {
            "id": "linux-a",
            "name": "A 服务器",
            "capabilities": ["shell.run"],
        },
    ]
    monkeypatch.setattr(core, "is_endpoint_agent_tool", lambda _tool: True)
    monkeypatch.setattr("api.devices.live.connected_agent_rows_for_user", lambda _user_id: agents)

    device_id, device_name = core._mcp_tool_device_identity(
        "shell.run",
        1,
        {"result": {"success": True, "deviceId": "linux-a", "result": {"stdout": "ok"}}},
    )

    assert device_id == "linux-a"
    assert device_name == "A 服务器"


def test_bubble_embeds_device_metadata_for_frontend():
    content = core._build_mcp_tool_bubble_content(
        "shell.run",
        {"command": "uptime"},
        '{"success": true}',
        device_id="linux-a",
        device_name="A 服务器",
    )

    assert "设备: A 服务器" in content
    assert "设备号: linux-a" in content
    assert content.index("设备号: linux-a") < content.index("[参数]")
