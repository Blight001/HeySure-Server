import asyncio

from api.services.chat import chat_realtime


class FakeUiSio:
    def __init__(self):
        self.calls = []

    async def emit(self, event, data, **kwargs):
        self.calls.append((event, data, kwargs))


def test_history_changed_targets_gateway_user_room(monkeypatch):
    fake = FakeUiSio()
    monkeypatch.setattr(chat_realtime, "ui_sio", fake)

    asyncio.run(chat_realtime.emit_history_changed(
        user_id=7,
        session_id="wechat_3_conn_peer",
        ai_config_id=3,
        ai_kind="core",
        message_id=91,
        source="wechat",
    ))

    event, payload, kwargs = fake.calls[0]
    assert event == "chat:history_changed"
    assert kwargs == {"room": "user_7"}
    assert payload == {
        "action": "append",
        "source": "wechat",
        "user_id": 7,
        "session_id": "wechat_3_conn_peer",
        "ai_config_id": 3,
        "ai_kind": "core",
        "message_id": 91,
    }
