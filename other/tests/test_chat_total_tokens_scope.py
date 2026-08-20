import asyncio
from types import SimpleNamespace

from gateway.routers import chat_history_routes


class _Result:
    def __init__(self, *, one=None, all_rows=None):
        self._one = one
        self._all = all_rows or []

    def one(self):
        return self._one

    def all(self):
        return self._all


class _Session:
    def __init__(self):
        self.statements = []

    def exec(self, statement):
        self.statements.append(statement)
        if len(self.statements) == 1:
            return _Result(one=(11, 7, 18, 2))
        return _Result(all_rows=[])


def _compiled_params(statement):
    return list(statement.compile().params.values())


def test_total_tokens_filters_persisted_and_live_usage_by_session(monkeypatch):
    session = _Session()
    monkeypatch.setattr(
        chat_history_routes,
        "get_current_user",
        lambda *_args, **_kwargs: SimpleNamespace(id=9),
    )

    result = asyncio.run(chat_history_routes.get_total_tokens(
        ai_config_id=3,
        ai_kind="core",
        session_id="session-selected",
        session=session,
        authorization="Bearer test",
    ))

    assert result["total_tokens"] == 18
    assert len(session.statements) == 2
    assert "session-selected" in _compiled_params(session.statements[0])
    assert "session-selected" in _compiled_params(session.statements[1])


def test_total_tokens_keeps_aggregate_mode_when_session_is_omitted(monkeypatch):
    session = _Session()
    monkeypatch.setattr(
        chat_history_routes,
        "get_current_user",
        lambda *_args, **_kwargs: SimpleNamespace(id=9),
    )

    asyncio.run(chat_history_routes.get_total_tokens(
        ai_config_id=3,
        ai_kind="core",
        session_id=None,
        session=session,
        authorization="Bearer test",
    ))

    persisted_sql = str(session.statements[0]).lower()
    live_sql = str(session.statements[1]).lower()
    assert "chatmessage.session_id =" not in persisted_sql
    assert "chatrun.session_id =" not in live_sql


def test_total_tokens_sql_repairs_legacy_inconsistent_total_column(monkeypatch):
    session = _Session()
    monkeypatch.setattr(
        chat_history_routes,
        "get_current_user",
        lambda *_args, **_kwargs: SimpleNamespace(id=9),
    )

    asyncio.run(chat_history_routes.get_total_tokens(
        session=session,
        authorization="Bearer test",
    ))

    persisted_sql = str(session.statements[0]).lower()
    assert "case" in persisted_sql
    assert "prompt_tokens" in persisted_sql
    assert "completion_tokens" in persisted_sql
