from types import SimpleNamespace

from gateway.routers import bots


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
