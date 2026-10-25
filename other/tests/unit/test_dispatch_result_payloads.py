import json
from types import SimpleNamespace

from connector_runtime.dispatch import result_payloads
from connector_runtime.dispatch import dispatch_results


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


def test_screenshot_dispatch_persistence_never_writes_data_url(monkeypatch):
    persisted = {}
    monkeypatch.setattr(
        dispatch_results,
        "_dispatch_module",
        lambda: SimpleNamespace(
            _device_kind_label=lambda _device_id: "桌面端",
            _update_agent_task_state=lambda *_args, **_kwargs: None,
        ),
    )
    monkeypatch.setattr(
        dispatch_results.repository,
        "finalize_dispatch_row",
        lambda task_id, **kwargs: persisted.update(kwargs),
    )
    envelope = dispatch_results.ResultEnvelope(
        task_id="atask_1",
        context={"suppress_session_message": True},
        device_id="desktop-1",
        success=True,
        tool="desktop_screenshot",
        summary="",
        result={"dataUrl": "data:image/png;base64,AAAA", "file_ref": "file_" + "a" * 32},
        screenshot=True,
    )

    dispatch_results._persist_result(envelope)

    assert persisted["result"]["dataUrl"].startswith("<image data URL omitted")
    assert persisted["result"]["file_ref"].startswith("file_")
