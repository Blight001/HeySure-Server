import time

import pytest
from sqlalchemy import event, text
from sqlalchemy.exc import DBAPIError

from api.core.settings import settings
from api.database import create_db_and_tables, engine, get_session


pytestmark = pytest.mark.integration


def test_runtime_startup_is_read_only_after_migration():
    statements = []

    def capture(_conn, _cursor, statement, _parameters, _context, _executemany):
        statements.append(statement.strip().upper())

    event.listen(engine, "before_cursor_execute", capture)
    try:
        create_db_and_tables()
    finally:
        event.remove(engine, "before_cursor_execute", capture)

    forbidden = ("ALTER ", "CREATE ", "DROP ", "TRUNCATE ")
    assert statements
    assert not [statement for statement in statements if statement.startswith(forbidden)]


def test_long_read_transaction_does_not_block_runtime_schema_guard():
    with engine.connect() as blocker:
        transaction = blocker.begin()
        blocker.execute(text('SELECT id FROM "user" LIMIT 1')).all()
        started = time.monotonic()
        create_db_and_tables()
        elapsed = time.monotonic() - started
        transaction.rollback()
    assert elapsed < 2.0


def test_database_lock_wait_is_bounded_by_connection_policy():
    lock_key = 7_314_159
    with engine.connect() as blocker, engine.connect() as waiter:
        blocker.execute(text("SELECT pg_advisory_lock(:key)"), {"key": lock_key})
        try:
            started = time.monotonic()
            with pytest.raises(DBAPIError):
                waiter.execute(
                    text("SELECT pg_advisory_lock(:key)"),
                    {"key": lock_key},
                )
            elapsed = time.monotonic() - started
            waiter.rollback()
        finally:
            blocker.execute(
                text("SELECT pg_advisory_unlock(:key)"),
                {"key": lock_key},
            )
    assert elapsed < 2.0


def test_runtime_rejects_deprecated_auto_migration_without_sql(monkeypatch):
    monkeypatch.setattr(settings, "db_auto_migrate", True)
    with pytest.raises(RuntimeError, match="no longer supported"):
        create_db_and_tables()


def test_request_exception_rolls_back_session():
    dependency = get_session()
    session = next(dependency)
    session.exec(text("SELECT 1")).one()
    with pytest.raises(RuntimeError, match="boom"):
        dependency.throw(RuntimeError("boom"))

    with engine.connect() as connection:
        idle = connection.execute(
            text(
                "SELECT count(*) FROM pg_stat_activity "
                "WHERE datname = current_database() "
                "AND state = 'idle in transaction' AND pid <> pg_backend_pid()"
            )
        ).scalar_one()
    assert idle == 0
