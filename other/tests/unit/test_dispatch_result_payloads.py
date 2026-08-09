import json

from connector_runtime.dispatch import result_payloads


def test_cookie_snapshot_is_persisted_but_removed_from_dispatch_result(tmp_path, monkeypatch):
    monkeypatch.setattr(result_payloads, "_workspace_dir", lambda _user_id, _ai_id: str(tmp_path))
    monkeypatch.setattr("api.core.config.user_workspace_dir", lambda _user_id: str(tmp_path))
    source = {
        "success": True,
        "data": {
            "account": "user@example.test",
            "password": "secret",
            "cookies": [{"name": "session", "value": "secret-cookie"}],
        },
    }

    cleaned = result_payloads.persist_cookies_result(user_id=1, ai_config_id=2, result=source)

    assert cleaned["saved_to_server"] is True
    assert "data" not in cleaned
    snapshot = json.loads((tmp_path / cleaned["workspace_path"]).read_text(encoding="utf-8"))
    assert snapshot["cookies"][0]["name"] == "session"
    assert snapshot["password"] == "secret"


def test_screenshot_bytes_are_replaced_with_size_marker():
    output = result_payloads.omit_screenshot_bytes(
        {"nested": {"dataUrl": "data:image/png;base64,AAAA"}, "server_path": "/safe/path"}
    )

    assert output["nested"]["dataUrl"].startswith("<image data URL omitted")
    assert output["server_path"] == "/safe/path"
