from types import SimpleNamespace

from api.services.chat import chat_inject


class _Session:
    def __init__(self):
        self.added = []
        self.commits = 0

    def add(self, row):
        self.added.append(row)

    def commit(self):
        self.commits += 1


def test_mark_pending_inject_preserves_bot_dedupe_tag():
    session = _Session()
    message = SimpleNamespace(tags="qq_inbound:provider-message")

    chat_inject.mark_message_pending_inject(session, message)

    assert message.tags == "qq_inbound:provider-message,pending_user_inject"
    assert session.added == [message]
    assert session.commits == 1


def test_pending_marker_is_idempotent_and_restores_original_tags():
    session = _Session()
    message = SimpleNamespace(tags="wechat_inbound:42,pending_user_inject")

    chat_inject.mark_message_pending_inject(session, message)

    assert message.tags.count(chat_inject.PENDING_INJECT_TAG) == 1
    assert chat_inject._clear_pending_tag(message.tags) == "wechat_inbound:42"


def test_plain_web_inject_clears_to_empty_tag():
    assert chat_inject._clear_pending_tag(chat_inject.PENDING_INJECT_TAG) == ""
