from types import SimpleNamespace

from connector_runtime.bots.qq import router as qq_router
from gateway.routers import bots


class _FirstResult:
    def first(self):
        return None


class _FakeSession:
    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, traceback):
        return False

    def exec(self, _statement):
        return _FirstResult()


def test_diagnose_uses_connector_owned_long_connection_state(monkeypatch):
    cfg = SimpleNamespace(id=19)
    user = SimpleNamespace(id=7)
    local_result = {
        "success": True,
        "connection_mode": "botpy_websocket",
        "bot_status": {"status": "failed"},
        "status": "failed",
        "ok": False,
    }
    fake_bot = SimpleNamespace(diagnose=lambda *_args, **_kwargs: dict(local_result))

    monkeypatch.setattr(bots, "_resolve_user_cfg", lambda *_args: (user, cfg))
    monkeypatch.setattr(bots, "get_bot", lambda channel: fake_bot if channel == "qq" else None)
    monkeypatch.setattr(bots.settings, "connector_runtime_url", "http://connector-runtime:3002")
    monkeypatch.setattr(
        bots,
        "_load_connector_bot_status",
        lambda *_args: (
            {
                "status": "success",
                "mode": "long_connection",
                "label": "运行中",
                "message": "botpy 长连接运行中",
            },
            None,
        ),
    )

    result = bots.diagnose_bot("qq", 19, object(), "Bearer test")

    assert result["status"] == "success"
    assert result["bot_status"]["message"] == "botpy 长连接运行中"
    assert result["ok"] is True


def test_external_mcp_qq_inbound_registers_route_without_starting_model(monkeypatch):
    cfg = SimpleNamespace(
        id=19,
        user_id=7,
        bot_channel="qq",
        ai_role="digital_member",
        execution_mode="external_mcp",
    )
    registered = []
    queued = []
    event = {
        "target_id": "qq-user-id",
        "target_type": "c2c",
        "message_id": "message-id",
        "event_id": "event-id",
        "text": "hello",
    }

    monkeypatch.setattr(qq_router, "Session", lambda _engine: _FakeSession())
    monkeypatch.setattr(qq_router, "get_ai_config_or_404", lambda *_args: cfg)
    monkeypatch.setattr(qq_router, "read_qq_config", lambda _cfg: {"enabled": True})
    monkeypatch.setattr(qq_router, "parse_qq_text_event", lambda _payload: event)
    monkeypatch.setattr(qq_router, "get_active_session_id", lambda *_args, **kwargs: kwargs["default"])
    monkeypatch.setattr(
        qq_router,
        "register_qq_session_route",
        lambda _session, **kwargs: registered.append(kwargs),
    )
    monkeypatch.setattr(
        qq_router,
        "_resolve_ai_runtime",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("model runtime must not start")),
    )
    monkeypatch.setattr(
        qq_router,
        "enqueue_external_mcp_message",
        lambda _session, _cfg, **kwargs: queued.append(kwargs) or SimpleNamespace(turn_id="xturn-1"),
    )

    result = qq_router.handle_qq_event_payload(19, {"op": 0, "t": "C2C_MESSAGE_CREATE"})

    assert result == {
        "op": 12,
        "d": 0,
        "external_controlled": True,
        "route_registered": True,
        "message_queued": True,
        "turn_id": "xturn-1",
    }
    assert len(registered) == 1
    assert registered[0]["target_id"] == "qq-user-id"
    assert registered[0]["ai_config_id"] == 19
    assert queued[0]["text"] == "hello"
