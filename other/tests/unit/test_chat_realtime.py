import asyncio
from unittest.mock import AsyncMock

from api.chat_runtime import chat_prompt_utils
from api.models import ChatMessageCreate
from api.services.chat import chat_realtime
from api.services.chat import chat_persistence
from connector_runtime.bots import notify as bot_notify


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


def test_run_live_event_identifies_external_conversation(monkeypatch):
    emit = AsyncMock()
    monkeypatch.setattr(chat_prompt_utils.sio, "emit", emit)
    run_id = "run_external_bot"
    with chat_prompt_utils._RUN_STATE_LOCK:
        chat_prompt_utils._RUN_LIVE_STATE[run_id] = {
            "text": "reply",
            "updated_at": 10.0,
        }
        chat_prompt_utils._RUN_LIVE_META[run_id] = {
            "user_id": 7,
            "session_id": "qq_3_conn_peer",
            "ai_config_id": 3,
            "ai_kind": "core",
        }

    async def emit_once():
        chat_prompt_utils._emit_run_live_update(run_id, force=True)
        await asyncio.sleep(0)

    try:
        asyncio.run(emit_once())
        payload = emit.await_args.args[1]
        assert payload["session_id"] == "qq_3_conn_peer"
        assert payload["ai_config_id"] == 3
        assert payload["ai_kind"] == "core"
    finally:
        with chat_prompt_utils._RUN_STATE_LOCK:
            chat_prompt_utils._RUN_LIVE_STATE.pop(run_id, None)
            chat_prompt_utils._RUN_LIVE_META.pop(run_id, None)


class FakeSession:
    def add(self, _row):
        return None

    def commit(self):
        return None

    def refresh(self, row):
        row.id = 42


def test_saved_message_notifies_open_browser_conversation(monkeypatch):
    calls = []
    monkeypatch.setattr(chat_persistence, "_upsert_session", lambda *_args: None)
    monkeypatch.setattr(chat_persistence, "_append_usage_snapshot", lambda **_kwargs: None)
    monkeypatch.setattr(chat_realtime, "notify_history_changed", lambda **kwargs: calls.append(kwargs))
    monkeypatch.setattr(bot_notify, "notify_saved_assistant_message", lambda *_args: None)

    saved = chat_persistence._save_message(
        FakeSession(),
        7,
        ChatMessageCreate(
            role="assistant",
            content="reply",
            ai_config_id=3,
            ai_kind="core",
            session_id="qq_3_conn_peer",
        ),
    )

    assert saved.id == 42
    assert calls == [{
        "user_id": 7,
        "session_id": "qq_3_conn_peer",
        "ai_config_id": 3,
        "ai_kind": "core",
        "message_id": 42,
        "source": "persistence",
    }]
