import pytest
from unittest.mock import Mock

from api import database
from api.core.settings import settings
from api.database import _require_assistant_avatar_column


class _ScalarResult:
    def __init__(self, value: bool):
        self._value = value

    def scalar_one(self) -> bool:
        return self._value


class _Connection:
    def __init__(self, exists: bool):
        self.exists = exists
        self.statements: list[str] = []

    def exec_driver_sql(self, statement: str) -> _ScalarResult:
        self.statements.append(statement)
        return _ScalarResult(self.exists)


class _ConnectionContext:
    def __init__(self, connection: _Connection):
        self.connection = connection

    def __enter__(self) -> _Connection:
        return self.connection

    def __exit__(self, *_args) -> None:
        return None


class _Engine:
    def __init__(self, exists: bool):
        self.connection = _Connection(exists)

    def connect(self) -> _ConnectionContext:
        return _ConnectionContext(self.connection)


def test_runtime_schema_guard_is_read_only_when_avatar_exists():
    engine = _Engine(exists=True)

    _require_assistant_avatar_column(engine)

    assert len(engine.connection.statements) == 1
    assert "SELECT EXISTS" in engine.connection.statements[0]
    assert "ALTER TABLE" not in engine.connection.statements[0]


def test_runtime_schema_guard_fails_fast_instead_of_running_ddl():
    engine = _Engine(exists=False)

    with pytest.raises(RuntimeError, match="api.db migrate"):
        _require_assistant_avatar_column(engine)

    assert len(engine.connection.statements) == 1
    assert "ALTER TABLE" not in engine.connection.statements[0]


def test_runtime_startup_rejects_schema_revision_mismatch(monkeypatch):
    monkeypatch.setattr(settings, "db_auto_migrate", False)
    monkeypatch.setattr("api.db._db_state", lambda _engine: (True, True))
    monkeypatch.setattr("api.db.expected_schema_revisions", lambda: {"head-new"})
    monkeypatch.setattr("api.db.current_schema_revisions", lambda _engine: {"head-old"})

    with pytest.raises(RuntimeError, match="current=.*head-old.*expected=.*head-new"):
        database.create_db_and_tables()


def test_runtime_startup_accepts_exact_schema_revision(monkeypatch):
    monkeypatch.setattr(settings, "db_auto_migrate", False)
    monkeypatch.setattr("api.db._db_state", lambda _engine: (True, True))
    monkeypatch.setattr("api.db.expected_schema_revisions", lambda: {"head"})
    monkeypatch.setattr("api.db.current_schema_revisions", lambda _engine: {"head"})
    avatar_guard = Mock()
    monkeypatch.setattr(database, "_require_assistant_avatar_column", avatar_guard)

    database.create_db_and_tables()

    avatar_guard.assert_called_once_with(database.engine)
