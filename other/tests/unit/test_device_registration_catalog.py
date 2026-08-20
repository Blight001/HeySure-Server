import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

from api.devices.catalog import DeviceCatalogError
from connector_runtime.socket_handlers import registration


def _context(info):
    return registration.Registration(
        sid="sid-1",
        info=info,
        device_id="device-1",
        user_id=1,
        account="owner",
        ai_config_id=None,
        ai_config_ids=(),
    )


def test_catalog_failure_does_not_publish_a_live_agent(monkeypatch):
    info = {
        "id": "device-1",
        "deviceType": "custom",
        "capabilities": ["demo.run"],
        "toolDefs": [{"name": "demo.run", "input_schema": {"type": "object"}}],
    }
    ctx = _context(info)
    monkeypatch.setattr(registration, "_registration", AsyncMock(return_value=ctx))
    monkeypatch.setattr(
        registration,
        "_record_presence",
        lambda _ctx: (_ for _ in ()).throw(DeviceCatalogError("DEVICE_CATALOG_TEST", "rejected")),
    )
    store = Mock()
    monkeypatch.setattr(registration, "_store_live_agent", store)
    emit = AsyncMock()
    monkeypatch.setattr(registration.sio, "emit", emit)

    asyncio.run(registration.handle_agent_register("sid-1", info))

    store.assert_not_called()
    payload = emit.await_args_list[0].args[1]
    assert payload["error_code"] == "DEVICE_CATALOG_TEST"


def test_success_acknowledges_server_catalog_generation_after_persist(monkeypatch):
    info = {
        "id": "device-1",
        "deviceType": "custom",
        "capabilities": ["demo.run"],
        "toolDefs": [{"name": "demo.run", "input_schema": {"type": "object"}}],
    }
    ctx = _context(info)
    monkeypatch.setattr(registration, "_registration", AsyncMock(return_value=ctx))
    monkeypatch.setattr(
        registration,
        "_record_presence",
        lambda _ctx: ({}, {
            "catalog_generation": 7,
            "catalog_hash": "a" * 64,
            "catalog_protocol_version": 2,
        }),
    )
    monkeypatch.setattr(registration, "_store_live_agent", lambda _ctx: None)
    monkeypatch.setattr(registration, "_push_pending_user_notifications", AsyncMock())
    monkeypatch.setattr(registration, "_push_dynamic_tools", AsyncMock())
    monkeypatch.setattr(registration, "_resume_owned_work", AsyncMock())
    emit = AsyncMock()
    monkeypatch.setattr(registration.sio, "emit", emit)

    asyncio.run(registration.handle_agent_register("sid-1", info))

    registered = next(call.args[1] for call in emit.await_args_list if call.args[0] == "device:registered")
    assert registered["catalogGeneration"] == 7
    assert registered["catalogHash"] == "a" * 64


def test_published_catalog_keeps_remote_transport_capabilities():
    info = {
        "capabilities": ["demo.run", "remote_control", "remote_terminal"],
        "toolDefs": [],
    }
    ctx = _context(info)
    catalog = SimpleNamespace(
        capabilities=("demo.run",),
        tool_defs=(),
        reported_ai_description="",
        protocol_version=2,
    )

    registration._publish_committed_catalog(ctx, catalog, {
        "catalog_generation": 3,
        "catalog_hash": "b" * 64,
        "catalog_protocol_version": 2,
    })

    assert ctx.info["capabilities"] == ["demo.run", "remote_control", "remote_terminal"]
