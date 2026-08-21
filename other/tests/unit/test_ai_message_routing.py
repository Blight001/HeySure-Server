from types import SimpleNamespace

import pytest

from ai_runtime.inference import ai_message_routing


class _Result:
    def __init__(self, row):
        self._row = row

    def first(self):
        return self._row


class _Session:
    def __init__(self, row):
        self.row = row

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def exec(self, _statement):
        return _Result(self.row)

    def add(self, row):
        assert row is self.row

    def commit(self):
        return None

    def refresh(self, row):
        assert row is self.row


@pytest.mark.parametrize("explicit_message_id", [True, False])
def test_send_to_reply_paths_record_reply_time(monkeypatch, explicit_message_id):
    row = SimpleNamespace(message_id="mai_1", replied_at=None, status="pending", reply_content=None)
    monkeypatch.setattr(ai_message_routing, "Session", lambda _engine: _Session(row))
    monkeypatch.setattr(
        ai_message_routing,
        "_row_to_dict",
        lambda item: {
            "message_id": item.message_id,
            "replied_at": item.replied_at,
            "status": item.status,
            "reply_content": item.reply_content,
        },
    )
    monkeypatch.setattr(ai_message_routing._pending_replies, "resolve", lambda *_args: True)

    if explicit_message_id:
        result = ai_message_routing.resolve_waiting_reply_to_message_id_from_send_message(
            user_id=1,
            current_ai_config_id=2,
            target_ai_config_id=3,
            message_id="mai_1",
            content="done",
        )
    else:
        result = ai_message_routing.resolve_waiting_reply_from_send_message(
            user_id=1,
            current_ai_config_id=2,
            target_ai_config_id=3,
            current_session_id="session_1",
            content="done",
        )

    assert result is not None
    assert result["status"] == "replied"
    assert result["reply_content"] == "done"
    assert isinstance(result["replied_at"], float)
    assert result["waiter_resolved"] is True
