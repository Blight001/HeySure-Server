import json
from types import SimpleNamespace

from connector_runtime.bots.messaging import DeliveryResult, Recipient
from mcp_runtime.mcp import registry as _registry  # noqa: F401 - initialize tools before direct import
from tools import communication


class _Result:
    def __init__(self, rows):
        self._rows = list(rows)

    def first(self):
        return self._rows[0] if self._rows else None

    def all(self):
        return list(self._rows)


class _Session:
    def __init__(self, result_rows):
        self._result_rows = list(result_rows)

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def exec(self, _stmt):
        return _Result(self._result_rows.pop(0))


def _cfg(*, default_target_id="", enabled=True):
    return SimpleNamespace(
        id=9,
        user_id=3,
        ai_role="digital_member",
        bot_channel="qq",
        bot_configs=json.dumps(
            {
                "qq": {
                    "enabled": enabled,
                    "app_id": "qq-app",
                    "app_secret": "qq-secret",
                    "default_target_id": default_target_id,
                    "default_target_type": "c2c",
                }
            }
        ),
    )


def _route(session_id, target_id, target_type="c2c", updated_at=1.0):
    return SimpleNamespace(
        session_id=session_id,
        target_json=json.dumps({"target_id": target_id, "target_type": target_type}),
        updated_at=updated_at,
    )


def test_qq_notification_uses_current_session_binding_first(monkeypatch):
    fake_session = _Session([
        [_cfg(default_target_id="configured-default")],
        [_route("qq-session", "current-openid")],
    ])
    monkeypatch.setattr(communication, "Session", lambda _engine: fake_session)
    monkeypatch.setattr(
        communication,
        "get_run_session_context",
        lambda: {"session_id": "qq-session", "ai_kind": "core"},
    )

    recipient, source, unavailable = communication._resolve_qq_notification_recipient(3, 9)

    assert unavailable is None
    assert recipient == Recipient(to_id="current-openid", to_type="c2c")
    assert source == "current_qq_session"


def test_qq_notification_falls_back_to_recent_binding_without_ids(monkeypatch):
    fake_session = _Session([
        [_cfg()],
        [],
        [_route("older-session", "recent-openid", "group")],
    ])
    monkeypatch.setattr(communication, "Session", lambda _engine: fake_session)
    monkeypatch.setattr(
        communication,
        "get_run_session_context",
        lambda: {"session_id": "web-task-session", "ai_kind": "core"},
    )

    recipient, source, unavailable = communication._resolve_qq_notification_recipient(3, 9)

    assert unavailable is None
    assert recipient == Recipient(to_id="recent-openid", to_type="group")
    assert source == "recent_qq_binding"


def test_qq_notification_returns_clear_result_when_no_receiver_is_bound(monkeypatch):
    fake_session = _Session([
        [_cfg()],
        [],
        [],
    ])
    monkeypatch.setattr(communication, "Session", lambda _engine: fake_session)
    monkeypatch.setattr(
        communication,
        "get_run_session_context",
        lambda: {"session_id": "web-task-session", "ai_kind": "core"},
    )

    recipient, source, unavailable = communication._resolve_qq_notification_recipient(3, 9)

    assert recipient is None
    assert source == ""
    assert unavailable["delivered"] is False
    assert unavailable["reason"] == "qq_recipient_not_bound"
    assert "未绑定 QQ" in unavailable["message"]


def test_qq_notification_returns_unbound_result_instead_of_sending(monkeypatch):
    monkeypatch.setattr(communication.dispatcher, "resolve_channel", lambda *_args: "qq")
    monkeypatch.setattr(
        communication.dispatcher,
        "resolve_bot",
        lambda _channel: SimpleNamespace(parse_recipient=lambda _raw: Recipient()),
    )
    monkeypatch.setattr(
        communication,
        "_resolve_qq_notification_recipient",
        lambda *_args: (
            None,
            "",
            {
                "delivered": False,
                "channel": "qq",
                "reason": "qq_recipient_not_bound",
                "message": "当前 AI 尚未绑定 QQ 接收用户或会话。",
            },
        ),
    )
    monkeypatch.setattr(
        communication.dispatcher,
        "send_text",
        lambda **_kwargs: (_ for _ in ()).throw(AssertionError("must not send")),
    )
    monkeypatch.setattr(communication, "_persist_user_notification", lambda **_kwargs: "notice_test")

    result = communication._user_send_message(3, {"text": "任务完成"}, 9)

    assert result["accepted"] is True
    assert result["delivered"] is False
    assert result["pending"] is True
    assert result["fallback_used"] is True
    assert result["fallback_reason"] == "qq_recipient_not_bound"
    assert result["notification_id"] == "notice_test"


def test_file_ref_attachment_is_resolved_without_exposing_absolute_path(monkeypatch):
    monkeypatch.setattr(
        communication,
        "resolve_file_ref",
        lambda **_kwargs: {
            "server_path": "/workspace/report.pdf",
            "file_name": "report.pdf",
        },
    )

    payloads = communication._attachment_payloads(3, 9, {
        "attachments": [{"file_ref": "file_" + "a" * 32}],
    })

    assert len(payloads) == 1
    assert payloads[0].path == "/workspace/report.pdf"
    assert payloads[0].file_name == "report.pdf"


def test_multiple_attachments_send_caption_only_once(monkeypatch):
    sent = []
    monkeypatch.setattr(communication.dispatcher, "resolve_channel", lambda *_args: "feishu")
    monkeypatch.setattr(
        communication.dispatcher,
        "send_media",
        lambda **kwargs: sent.append(kwargs["media"]) or DeliveryResult(
            ok=True,
            channel="feishu",
            detail={"message_id": f"m{len(sent)}"},
        ),
    )
    monkeypatch.setattr(
        communication,
        "resolve_file_ref",
        lambda **kwargs: {
            "server_path": f"/workspace/{kwargs['file_ref']}.bin",
            "file_name": f"{kwargs['file_ref']}.bin",
        },
    )
    monkeypatch.setattr(communication, "_persist_user_notification", lambda **_kwargs: "notice_test")

    result = communication._user_send_message(3, {
        "to": "user",
        "text": "两个附件",
        "attachments": ["file_" + "a" * 32, "file_" + "b" * 32],
    }, 9)

    assert result["delivered"] is True
    assert result["attachment_count"] == 2
    assert result["message_ids"] == ["m1", "m2"]
    assert [item.text for item in sent] == ["两个附件", ""]


def test_multiple_attachments_report_partial_delivery_without_false_success(monkeypatch):
    calls = []
    monkeypatch.setattr(communication.dispatcher, "resolve_channel", lambda *_args: "feishu")

    def send_media(**kwargs):
        calls.append(kwargs["media"])
        if len(calls) == 2:
            raise RuntimeError("second upload failed")
        return DeliveryResult(ok=True, channel="feishu", detail={"message_id": "m1"})

    monkeypatch.setattr(communication.dispatcher, "send_media", send_media)
    monkeypatch.setattr(
        communication,
        "resolve_file_ref",
        lambda **kwargs: {
            "server_path": f"/workspace/{kwargs['file_ref']}.bin",
            "file_name": "file.bin",
        },
    )
    monkeypatch.setattr(communication, "_persist_user_notification", lambda **_kwargs: "notice_test")

    result = communication._user_send_message(3, {
        "text": "附件",
        "attachments": ["file_" + "a" * 32, "file_" + "b" * 32],
    }, 9)

    assert result["delivered"] is False
    assert result["accepted"] is True
    assert result["fallback_used"] is True
    assert result["partial"] is True
    assert result["sent_count"] == 1
    assert result["attachment_count"] == 2


def test_bot_delivery_is_archived_without_duplicate_app_push(monkeypatch):
    captured = {}
    monkeypatch.setattr(communication.dispatcher, "resolve_channel", lambda *_args: "feishu")
    monkeypatch.setattr(
        communication.dispatcher,
        "send_text",
        lambda **_kwargs: DeliveryResult(ok=True, channel="feishu", detail={"message_id": "m1"}),
    )
    monkeypatch.setattr(
        communication,
        "_persist_user_notification",
        lambda **kwargs: captured.update(kwargs) or "notice_test",
    )

    result = communication._user_send_message(3, {"text": "完成通知"}, 9)

    assert result["accepted"] is True
    assert result["delivered"] is True
    assert result["pending"] is False
    assert result["fallback_used"] is False
    assert captured["external_delivered"] is True


def test_bot_transport_failure_falls_back_without_raising(monkeypatch):
    captured = {}
    monkeypatch.setattr(communication.dispatcher, "resolve_channel", lambda *_args: "feishu")
    monkeypatch.setattr(
        communication.dispatcher,
        "send_text",
        lambda **_kwargs: (_ for _ in ()).throw(RuntimeError("upstream unavailable")),
    )
    monkeypatch.setattr(
        communication,
        "_persist_user_notification",
        lambda **kwargs: captured.update(kwargs) or "notice_test",
    )

    result = communication._user_send_message(3, {"text": "完成通知"}, 9)

    assert result["accepted"] is True
    assert result["delivered"] is False
    assert result["delivery_status"] == "app_pending"
    assert result["fallback_reason"] == "RuntimeError"
    assert captured["external_delivered"] is False


def test_qq_local_media_uses_temporary_public_url(monkeypatch, tmp_path):
    from tools import communication

    media = tmp_path / "video.mp4"
    media.write_bytes(b"x" * 1024)
    monkeypatch.setattr(communication, "get_project_root", lambda *_args: str(tmp_path))
    monkeypatch.setattr(
        communication, "register_workspace_file",
        lambda **_kwargs: {"file_ref": "file_video"},
    )
    monkeypatch.setattr(communication, "configured_public_base_url", lambda: "https://public.example")
    monkeypatch.setattr(
        communication, "create_temporary_file_link",
        lambda **_kwargs: {"url": "https://public.example/api/tmp-files/grant/token"},
    )

    payload = communication._legacy_media_payload(
        1, 4, {"media_path": "video.mp4", "media_type": "video"}, channel="qq"
    )

    assert payload.path == str(media)
    assert payload.url == "https://public.example/api/tmp-files/grant/token"
