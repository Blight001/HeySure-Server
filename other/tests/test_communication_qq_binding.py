import json
from types import SimpleNamespace

from connector_runtime.bots.messaging import Recipient
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

    result = communication._user_send_message(3, {"text": "任务完成"}, 9)

    assert result["delivered"] is False
    assert result["reason"] == "qq_recipient_not_bound"
    assert "未绑定 QQ" in result["message"]
