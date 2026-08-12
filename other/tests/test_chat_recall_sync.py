import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

from gateway.routers import chat_action_routes


class _Rows:
    def __init__(self, rows):
        self._rows = rows

    def all(self):
        return list(self._rows)


class _Session:
    def __init__(self, message):
        self.message = message
        self.deleted = []
        self.committed = False

    def get(self, _model, _message_id):
        return self.message

    def exec(self, _statement):
        return _Rows([self.message])

    def delete(self, message):
        self.deleted.append(message)

    def commit(self):
        self.committed = True


def test_recall_broadcasts_scoped_history_change(monkeypatch):
    message = SimpleNamespace(
        id=17,
        user_id=3,
        session_id="session-sync",
        created_at=100.0,
        ai_kind="assistant",
        ai_config_id=12,
        content="撤回内容",
    )
    session = _Session(message)
    emitted = AsyncMock()
    monkeypatch.setattr(chat_action_routes, "get_current_user", lambda *_: SimpleNamespace(id=3))
    monkeypatch.setattr(chat_action_routes, "delete_message_media", lambda *_: None)
    monkeypatch.setattr(chat_action_routes, "_rebuild_usage_snapshots", lambda *_: None)
    monkeypatch.setattr(chat_action_routes.sio, "emit", emitted)

    result = asyncio.run(chat_action_routes.recall_chat_messages(17, session, "Bearer test"))

    assert result["success"] is True
    assert session.committed is True
    emitted.assert_awaited_once_with(
        "chat:history_changed",
        {
            "action": "recall",
            "user_id": 3,
            "session_id": "session-sync",
            "ai_config_id": 12,
            "ai_kind": "assistant",
            "from_message_id": 17,
        },
        room="user_3",
    )
