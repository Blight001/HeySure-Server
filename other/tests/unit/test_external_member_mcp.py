import asyncio
import json
import time
from types import SimpleNamespace

import pytest
from starlette.requests import Request

from api.models import AssistantAIConfig, ExternalMcpCredential
from api.services.mcp import external_access
from api.services.mcp import external_transport
from gateway.routers import external_mcp_protocol as protocol
from gateway.routers import external_mcp_settings as settings_routes


class _Rows:
    def __init__(self, value):
        self.value = value

    def first(self):
        return self.value

    def all(self):
        return self.value if isinstance(self.value, list) else [self.value]


class _Session:
    def __init__(self, credential=None, config=None):
        self.credential = credential
        self.config = config
        self.added = []
        self.commits = 0

    def exec(self, _statement):
        return _Rows(self.credential)

    def get(self, _model, _identifier):
        return self.config

    def add(self, row):
        self.added.append(row)

    def commit(self):
        self.commits += 1

    def rollback(self):
        pass

    def refresh(self, _row):
        pass


def _config(**overrides):
    values = {
        "id": 19,
        "user_id": 7,
        "name": "shared-member",
        "external_mcp_enabled": True,
        "external_mcp_public_id": "a" * 32,
        "enabled": True,
        "mcp_enabled": True,
        "lifecycle_status": "working",
        "ai_role": "digital_member",
    }
    values.update(overrides)
    return AssistantAIConfig(**values)


def _principal():
    return external_access.ExternalMcpPrincipal(
        credential_id=3,
        user_id=7,
        ai_config_id=19,
        public_id="a" * 32,
    )


def _request(headers=None, body=b"{}", path="/mcp/member"):
    raw_headers = [
        (str(key).lower().encode(), str(value).encode())
        for key, value in (headers or {}).items()
    ]
    sent = False

    async def receive():
        nonlocal sent
        if sent:
            return {"type": "http.request", "body": b"", "more_body": False}
        sent = True
        return {"type": "http.request", "body": body, "more_body": False}

    return Request({
        "type": "http",
        "method": "POST",
        "path": path,
        "scheme": "http",
        "server": ("127.0.0.1", 3000),
        "client": ("127.0.0.1", 52000),
        "headers": raw_headers,
    }, receive)


def test_credential_auth_uses_hash_and_updates_last_used():
    raw = "hsmcp_" + "secret" * 8
    row = ExternalMcpCredential(
        id=3,
        user_id=7,
        ai_config_id=19,
        token_hash=external_access.token_hash(raw),
        token_prefix=raw[:12],
    )
    session = _Session(row, _config())

    principal, cfg = external_access.authenticate_credential(
        session,
        f"Bearer {raw}",
        public_id="a" * 32,
    )

    assert principal.ai_config_id == 19
    assert cfg.name == "shared-member"
    assert row.last_used_at is not None
    assert session.commits == 1
    assert raw not in row.model_dump().values()


def test_recent_last_used_is_throttled():
    raw = "hsmcp_" + "secret" * 8
    row = ExternalMcpCredential(
        id=3,
        user_id=7,
        ai_config_id=19,
        token_hash=external_access.token_hash(raw),
        last_used_at=time.time(),
    )
    session = _Session(row, _config())
    external_access.authenticate_credential(session, f"Bearer {raw}")
    assert session.commits == 0


def test_credential_capacity_limits_active_rows():
    rows = [ExternalMcpCredential(
        id=index + 1,
        user_id=7,
        ai_config_id=19,
        token_hash=f"{index:064x}",
    ) for index in range(external_access.MAX_ACTIVE_CREDENTIALS_PER_MEMBER)]
    session = _Session(rows, _config())
    with pytest.raises(external_access.ExternalMcpCredentialLimitError):
        external_access.ensure_credential_capacity(session, _config())


@pytest.mark.parametrize(
    "row",
    [
        None,
        ExternalMcpCredential(
            id=3, user_id=7, ai_config_id=19, token_hash="x", revoked_at=time.time()
        ),
        ExternalMcpCredential(
            id=3, user_id=7, ai_config_id=19, token_hash="x", expires_at=time.time() - 1
        ),
    ],
)
def test_invalid_revoked_and_expired_credentials_are_http_401(row):
    session = _Session(row, _config())
    with pytest.raises(external_access.ExternalMcpAccessError) as caught:
        external_access.authenticate_credential(session, "Bearer invalid")
    assert caught.value.http_status == 401


@pytest.mark.parametrize(
    "changes,code",
    [
        ({"external_mcp_enabled": False}, "external_mcp_disabled"),
        ({"enabled": False}, "member_unavailable"),
        ({"lifecycle_status": "dead"}, "member_unavailable"),
        ({"mcp_enabled": False}, "member_mcp_disabled"),
    ],
)
def test_member_availability_is_fail_closed(changes, code):
    with pytest.raises(external_access.ExternalMcpAccessError) as caught:
        external_access.ensure_member_available(_config(**changes))
    assert caught.value.code == code


def test_tools_list_preserves_member_capability_contract(monkeypatch):
    capability = SimpleNamespace(
        description="Publish the current page",
        input_schema={"type": "object", "required": ["title"]},
        destructive=True,
    )
    view = SimpleNamespace(eligible={"browser.publish": capability})
    monkeypatch.setattr(
        "api.services.mcp.capability_view.scoped_tool_view_for_ids",
        lambda user_id, config_id: view,
    )

    result = protocol._tools_list(_principal())

    assert result == {"tools": [{
        "name": "browser.publish",
        "description": "Publish the current page",
        "inputSchema": {"type": "object", "required": ["title"]},
        "annotations": {"destructiveHint": True, "readOnlyHint": False},
    }]}


def test_tools_call_rechecks_eligibility_dispatches_and_records_stats(monkeypatch):
    observed = {}

    monkeypatch.setattr(
        "api.services.mcp.capability_view.ensure_tool_eligible",
        lambda user_id, config_id, tool: observed.setdefault(
            "guard", (user_id, config_id, tool)
        ),
    )

    async def fake_call(tool, user_id, arguments, config_id):
        observed["call"] = (tool, user_id, arguments, config_id)
        return {"tool": tool, "result": {"success": True, "value": 42}}

    monkeypatch.setattr(protocol, "call_mcp_or_endpoint_tool", fake_call)
    monkeypatch.setattr(
        protocol.mcp_stats,
        "record_call",
        lambda **kwargs: observed.setdefault("stats", kwargs),
    )

    outcome = asyncio.run(protocol._tools_call(
        _principal(),
        {"name": "workspace.search", "arguments": {"q": "MCP"}},
    ))

    assert observed["guard"] == (7, 19, "workspace.search")
    assert observed["call"] == ("workspace.search", 7, {"q": "MCP"}, 19)
    assert observed["stats"]["success"] is True
    assert outcome.payload["isError"] is False
    assert json.loads(outcome.payload["content"][0]["text"])["result"]["value"] == 42


def test_unknown_tool_is_invalid_params(monkeypatch):
    from fastapi import HTTPException

    monkeypatch.setattr(
        "api.services.mcp.capability_view.ensure_tool_eligible",
        lambda *_args: (_ for _ in ()).throw(HTTPException(status_code=403)),
    )
    monkeypatch.setattr(
        "api.services.mcp.capability_view.scoped_tool_view_for_ids",
        lambda *_args: SimpleNamespace(eligible={}, blocked={}),
    )
    monkeypatch.setattr(protocol, "is_known_external_tool", lambda *_args: False)

    with pytest.raises(protocol.RpcFault) as caught:
        asyncio.run(protocol._tools_call(
            _principal(), {"name": "not.real", "arguments": {}}
        ))
    assert caught.value.code == -32602


def test_known_but_revoked_tool_is_call_result_error(monkeypatch):
    from fastapi import HTTPException

    monkeypatch.setattr(
        "api.services.mcp.capability_view.ensure_tool_eligible",
        lambda *_args: (_ for _ in ()).throw(HTTPException(status_code=403)),
    )
    monkeypatch.setattr(
        "api.services.mcp.capability_view.scoped_tool_view_for_ids",
        lambda *_args: SimpleNamespace(eligible={}, blocked={"desktop_action": object()}),
    )
    monkeypatch.setattr(protocol.mcp_stats, "record_call", lambda **_kwargs: None)

    outcome = asyncio.run(protocol._tools_call(
        _principal(), {"name": "desktop_action", "arguments": {}}
    ))
    assert outcome.payload["isError"] is True
    assert outcome.error_code == "tool_unavailable"


def test_initialize_and_notification_contract(monkeypatch):
    monkeypatch.setattr(protocol, "record_audit", lambda *_args, **_kwargs: None)
    cfg = _config()
    initialized = asyncio.run(protocol._dispatch(
        "initialize",
        {
            "protocolVersion": "2025-03-26",
            "capabilities": {},
            "clientInfo": {"name": "Codex", "version": "1.0"},
        },
        _principal(),
    ))
    assert initialized.payload["protocolVersion"] == "2025-03-26"
    assert initialized.payload["capabilities"]["tools"] == {"listChanged": False}

    response = asyncio.run(protocol._dispatch_and_respond(
        _principal(),
        {"jsonrpc": "2.0", "method": "notifications/initialized"},
    ))
    assert response.status_code == 202


def test_credential_payload_exposes_only_metadata_with_iso_timestamps():
    row = ExternalMcpCredential(
        id=3,
        user_id=7,
        ai_config_id=19,
        token_hash="f" * 64,
        token_prefix="hsmcp_abcd",
        created_at=1_700_000_000,
    )
    payload = external_access.credential_payload(row)
    assert payload["created_at"].endswith("+00:00")
    assert "token_hash" not in payload
    assert "token" not in payload


def test_transport_headers_are_strict_and_allow_json_plus_sse():
    accepted = _request(headers={
        "content-type": "application/json; charset=utf-8",
        "accept": "application/json, text/event-stream",
        "origin": "http://127.0.0.1:3000",
    })
    assert protocol._validate_transport_headers(accepted) is None

    wrong_origin = _request(headers={
        "content-type": "application/json",
        "accept": "application/json, text/event-stream",
        "origin": "https://attacker.example",
    })
    assert protocol._validate_transport_headers(wrong_origin).status_code == 403

    wrong_type = _request(headers={"content-type": "text/plain", "accept": "application/json"})
    assert protocol._validate_transport_headers(wrong_type).status_code == 415

    json_only = _request(headers={"content-type": "application/json", "accept": "application/json"})
    assert protocol._validate_transport_headers(json_only).status_code == 406
    wildcard = _request(headers={"content-type": "application/json", "accept": "*/*"})
    assert protocol._validate_transport_headers(wildcard).status_code == 406


def test_protocol_version_is_required_after_initialize():
    no_version = _request(headers={})
    assert protocol._validate_protocol_version(
        no_version,
        {"jsonrpc": "2.0", "method": "tools/list", "id": 1},
    ) is None
    supported = _request(headers={"mcp-protocol-version": "2025-06-18"})
    assert protocol._validate_protocol_version(
        supported,
        {"jsonrpc": "2.0", "method": "tools/list", "id": 1},
    ) is None


def test_initialize_never_negotiates_legacy_http_transport():
    result = protocol._initialize_result({
        "protocolVersion": "2024-11-05",
        "capabilities": {},
        "clientInfo": {"name": "Codex", "version": "1.0"},
    })
    assert result["protocolVersion"] == "2025-06-18"
    with pytest.raises(protocol.RpcFault) as caught:
        protocol._initialize_result(None)
    assert caught.value.code == -32602


@pytest.mark.parametrize("bad_params", [
    {"protocolVersion": "2025-03-26", "capabilities": [], "clientInfo": {"name": "x", "version": "1"}},
    {"protocolVersion": "2025-03-26", "capabilities": {}, "clientInfo": {"name": "x"}},
    {"protocolVersion": "2025-03-26", "capabilities": {}, "clientInfo": {"name": "", "version": "1"}},
])
def test_initialize_requires_capabilities_and_complete_client_info(bad_params):
    with pytest.raises(protocol.RpcFault) as caught:
        protocol._initialize_result(bad_params)
    assert caught.value.code == -32602


def test_body_limit_rejects_declared_oversize_request():
    request = _request(headers={"content-length": str(protocol._MAX_REQUEST_BYTES + 1)})
    _message, response = asyncio.run(protocol._read_message(request))
    assert response.status_code == 413


def test_credential_concurrency_slot_releases_after_exit(monkeypatch):
    monkeypatch.setattr(external_transport, "MAX_CONCURRENT_CALLS_PER_CREDENTIAL", 1)
    monkeypatch.setattr(external_transport, "MAX_CONCURRENT_CALLS_PER_MEMBER", 4)
    external_transport._CALL_COUNTS.clear()
    with protocol._external_call_slot(3, 19) as first:
        with protocol._external_call_slot(3, 19) as second:
            assert first is True
            assert second is False
    with protocol._external_call_slot(3, 19) as after_release:
        assert after_release is True
    assert external_transport._CALL_COUNTS == {}


def test_member_concurrency_cannot_be_bypassed_with_more_credentials(monkeypatch):
    monkeypatch.setattr(external_transport, "MAX_CONCURRENT_CALLS_PER_CREDENTIAL", 2)
    monkeypatch.setattr(external_transport, "MAX_CONCURRENT_CALLS_PER_MEMBER", 1)
    external_transport._CALL_COUNTS.clear()
    with protocol._external_call_slot(3, 19) as first:
        with protocol._external_call_slot(4, 19) as second_credential:
            assert first is True
            assert second_credential is False
    assert external_transport._CALL_COUNTS == {}


def test_request_and_notification_id_semantics():
    assert protocol._validate_message_semantics({
        "jsonrpc": "2.0", "method": "tools/list", "id": "call-1",
    }) is None
    for invalid_id in (None, True, 1.5):
        fault = protocol._validate_message_semantics({
            "jsonrpc": "2.0", "method": "tools/list", "id": invalid_id,
        })
        assert fault.code == -32600
    missing = protocol._validate_message_semantics({
        "jsonrpc": "2.0", "method": "ping",
    })
    assert missing.code == -32600
    initialized = protocol._validate_message_semantics({
        "jsonrpc": "2.0", "method": "notifications/initialized",
    })
    assert initialized is None
    invalid_notification = protocol._validate_message_semantics({
        "jsonrpc": "2.0", "method": "notifications/initialized", "id": 1,
    })
    assert invalid_notification.code == -32600


def test_ip_and_credential_rate_limits_use_separate_buckets(monkeypatch):
    external_transport._RATE_EVENTS.clear()
    monkeypatch.setattr(external_transport, "CALL_RATE_PER_IP", 1)
    monkeypatch.setattr(external_transport, "CONTROL_RATE_PER_IP", 2)
    monkeypatch.setattr(external_transport, "CALL_RATE_PER_CREDENTIAL", 1)
    request = _request()
    assert external_transport.rate_limit_ip(request, "tools/call") is None
    limited_ip = external_transport.rate_limit_ip(request, "tools/call")
    assert limited_ip.status_code == 429
    assert int(limited_ip.headers["retry-after"]) >= 1
    assert external_transport.rate_limit_ip(request, "tools/list") is None
    assert external_transport.rate_limit_credential(3, "tools/call") is None
    limited_credential = external_transport.rate_limit_credential(3, "tools/call")
    assert limited_credential.status_code == 429


def test_tool_timeout_is_error_result_and_uses_sanitized_stats(monkeypatch):
    observed = {}

    async def slow_call(_principal, _params):
        await asyncio.sleep(0.02)

    monkeypatch.setattr(protocol, "_tools_call", slow_call)
    monkeypatch.setattr(protocol, "external_tool_timeout", lambda *_args: 0.001)
    monkeypatch.setattr(
        protocol.mcp_stats,
        "record_call",
        lambda **kwargs: observed.update(kwargs),
    )
    outcome = asyncio.run(protocol._dispatch_tool_call_with_timeout(
        _principal(), {"name": "slow.tool", "arguments": {}}
    ))
    assert outcome.payload["isError"] is True
    assert outcome.error_code == "tool_timeout"
    assert observed["error"] == "external_tool_timeout"


def test_settings_endpoint_uses_public_gateway_not_agent_socket(monkeypatch):
    monkeypatch.setattr(settings_routes.settings, "public_base_url", "https://heysure.example/")
    monkeypatch.setattr(settings_routes.settings, "agent_socket_url", "http://connector:3002")
    request = _request()
    assert settings_routes._external_base_url(request) == "https://heysure.example"


def test_public_host_requires_configured_public_base_url(monkeypatch):
    monkeypatch.setattr(settings_routes.settings, "public_base_url", "")
    request = _request()
    request.scope["server"] = ("attacker.example", 443)
    request.scope["scheme"] = "https"
    with pytest.raises(Exception) as caught:
        settings_routes._external_base_url(request)
    assert getattr(caught.value, "status_code", None) == 503


def test_settings_payload_includes_live_capability_summary_and_absolute_endpoint(monkeypatch):
    view = SimpleNamespace(eligible={"one": object(), "two": object()}, revision="rev-2")
    monkeypatch.setattr(
        "api.services.mcp.capability_view.scoped_tool_view_for_ids",
        lambda *_args: view,
    )
    payload = settings_routes._settings_payload(
        _config(),
        [],
        "https://heysure.example",
    )
    assert payload["endpoint"] == "https://heysure.example/mcp/member"
    assert payload["tool_count"] == 2
    assert payload["capability_revision"] == "rev-2"
    assert (
        f"tool_timeout_sec = {external_transport.codex_tool_timeout_seconds()}"
        in settings_routes._codex_config(payload["endpoint"])
    )


def test_endpoint_outer_timeout_follows_dispatch_deadline_with_safety(monkeypatch):
    monkeypatch.setattr(
        "connector_runtime.dispatch.desktop_device_tools.is_endpoint_agent_tool",
        lambda _tool: True,
    )
    monkeypatch.setattr(
        "ai_runtime.inference.runtime_clients.endpoint_dispatch_timeout",
        lambda _tool, _arguments: 300,
    )
    assert external_transport.external_tool_timeout("desktop_action", {}) == 330
    monkeypatch.setattr(
        "connector_runtime.dispatch.desktop_device_tools.is_endpoint_agent_tool",
        lambda _tool: False,
    )
    assert external_transport.external_tool_timeout("workspace.search", {}) == 180


def test_workflow_timeout_reuses_internal_mcp_deadline_and_codex_covers_it(monkeypatch):
    monkeypatch.setattr(
        "connector_runtime.dispatch.desktop_device_tools.is_endpoint_agent_tool",
        lambda _tool: False,
    )
    monkeypatch.setattr(
        "ai_runtime.inference.runtime_clients.mcp_call_timeout",
        lambda _tool, _arguments: 2100,
    )
    assert external_transport.external_tool_timeout(
        "automation.manage", {"action": "start"}
    ) == 2130
    expected_codex_timeout = max(
        external_transport.MAX_ENDPOINT_EXTERNAL_TIMEOUT_SECONDS + 30,
        int(external_transport.settings.workflow_chat_wait_timeout_seconds) + 360,
    )
    assert external_transport.codex_tool_timeout_seconds() == expected_codex_timeout
