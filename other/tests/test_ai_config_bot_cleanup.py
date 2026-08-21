from api.models import BotConnection, BotContact, BotSessionRoute, BotUserCursor
from api.services.bot_directory import delete_ai_bot_directory


class _Rows:
    def __init__(self, rows):
        self._rows = rows

    def all(self):
        return self._rows


class _Session:
    def __init__(self, rows_by_model):
        self.rows_by_model = rows_by_model
        self.deleted = []
        self.flush_count = 0

    def exec(self, statement):
        model = statement.column_descriptions[0]["entity"]
        return _Rows(self.rows_by_model.get(model, []))

    def delete(self, row):
        self.deleted.append((type(row), self.flush_count))

    def flush(self):
        self.flush_count += 1


def test_delete_ai_bot_directory_flushes_dependents_before_contacts_and_connections():
    route = BotSessionRoute(user_id=1, ai_config_id=19, channel="wechat", session_id="session")
    cursor = BotUserCursor(user_id=1, ai_config_id=19, channel="wechat", identity_key="opaque")
    contact = BotContact(user_id=1, ai_config_id=19, connection_id=7, contact_ref="contact")
    connection = BotConnection(user_id=1, ai_config_id=19, channel="wechat", connection_ref="conn")
    session = _Session({
        BotSessionRoute: [route],
        BotUserCursor: [cursor],
        BotContact: [contact],
        BotConnection: [connection],
    })

    delete_ai_bot_directory(session, user_id=1, ai_config_id=19)

    assert session.deleted == [
        (BotSessionRoute, 0),
        (BotUserCursor, 0),
        (BotContact, 1),
        (BotConnection, 2),
    ]
    assert session.flush_count == 3
