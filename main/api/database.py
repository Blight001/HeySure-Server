"""SQLModel engine + session dependency.

Schema is owned by Alembic (see ``api.db`` / ``other/migrations/`` /
``doc/db-migrations.md``). Runtime startup only verifies that the configured
database is already at the code version's Alembic head.
"""

import contextlib
import logging
import time
from typing import Iterator

from sqlmodel import Session, create_engine

from .core.config import DATABASE_URL

# Importing the models package side-effect populates ``SQLModel.metadata``.
from . import models  # noqa: F401
from .models import external_control as _external_control_models  # noqa: F401

logger = logging.getLogger(__name__)
_BOOTSTRAP_ADVISORY_LOCK_KEY = 518_329_771_405_339_013


@contextlib.contextmanager
def _bootstrap_lock():
    """Serialize schema/bootstrap work across concurrently starting services.

    Uses a *raw* psycopg connection (separate from the SQLAlchemy engine pool)
    to hold the advisory lock. This prevents the long-held lock connection from
    consuming a slot in the main pool or causing checkout waits/hangs during
    the subsequent _db_state() and Alembic upgrade (a common cause of "卡死"
    at "checking DB state").
    """
    import psycopg
    from .core.config import psycopg_dsn  # noqa: F401  (used below)

    deadline = time.time() + 120.0
    logger.info("Acquiring bootstrap advisory lock for DB schema...")
    # Use raw psycopg so we don't hold a pooled SA connection for the entire migration.
    lock_conn = psycopg.connect(psycopg_dsn(), autocommit=True)
    try:
        attempt = 0
        while True:
            attempt += 1
            locked = lock_conn.execute(
                f"SELECT pg_try_advisory_lock({_BOOTSTRAP_ADVISORY_LOCK_KEY})"
            ).fetchone()[0]
            if locked:
                logger.info("Acquired bootstrap lock (raw conn), running schema migration...")
                try:
                    yield
                finally:
                    try:
                        lock_conn.execute(
                            f"SELECT pg_advisory_unlock({_BOOTSTRAP_ADVISORY_LOCK_KEY})"
                        )
                        logger.info("Released bootstrap lock.")
                    except Exception:
                        logger.exception("failed to release postgres bootstrap lock")
                return
            if time.time() >= deadline:
                raise RuntimeError(
                    "database is busy; another process is still bootstrapping the Postgres database"
                )
            if attempt % 4 == 0:  # log every ~2s
                logger.info(f"Waiting for bootstrap lock (attempt {attempt})...")
            time.sleep(0.5)
    finally:
        try:
            lock_conn.close()
        except Exception:
            pass


# pool_pre_ping handles dropped connections after server restarts;
# pool_recycle prevents stale long-lived connections.
#
# pool_size / max_overflow are tunable because the gateway serves DB-bound routes
# as plain ``def`` handlers, which FastAPI runs in a worker threadpool: many
# requests execute truly concurrently, and each holds one connection for its
# duration. The default QueuePool of 5+10 throttles that concurrency (later
# requests block on checkout), so we size the pool from settings instead.
def _pool_kwargs() -> dict:
    try:
        from .core.settings import settings
        return {"pool_size": settings.db_pool_size, "max_overflow": settings.db_max_overflow}
    except Exception:  # settings unavailable (e.g. tooling import) → SQLAlchemy defaults
        return {}


def _connect_args() -> dict:
    from .core.settings import settings

    options = " ".join(
        [
            f"-c lock_timeout={max(1, settings.db_lock_timeout_ms)}ms",
            f"-c statement_timeout={max(1, settings.db_statement_timeout_ms)}ms",
            "-c idle_in_transaction_session_timeout="
            f"{max(1, settings.db_idle_transaction_timeout_ms)}ms",
        ]
    )
    return {
        "connect_timeout": max(1, settings.db_connect_timeout_seconds),
        "options": options,
    }


engine = create_engine(
    DATABASE_URL,
    pool_pre_ping=True,
    pool_recycle=300,
    connect_args=_connect_args(),
    **_pool_kwargs(),
)


def _require_assistant_avatar_column(db_engine) -> None:
    """Fail fast when the Alembic-owned compatibility column is missing.

    Runtime processes must never repair schema drift themselves. Even an
    idempotent PostgreSQL ``ALTER TABLE ... IF NOT EXISTS`` requests an ACCESS
    EXCLUSIVE lock and can block unrelated login/read traffic while it waits.
    """
    with db_engine.connect() as conn:
        avatar_exists = bool(
            conn.exec_driver_sql(
                """
                SELECT EXISTS (
                    SELECT 1
                    FROM information_schema.columns
                    WHERE table_schema = current_schema()
                      AND table_name = 'assistantaiconfig'
                      AND column_name = 'avatar'
                )
                """
            ).scalar_one()
        )
    if not avatar_exists:
        raise RuntimeError(
            "database schema is missing assistantaiconfig.avatar; "
            "run `python -m api.db migrate` before starting runtime services"
        )


def create_db_and_tables() -> None:
    """Read-only runtime schema guard (historical name kept for callers).

    Runtime processes are never migration owners. Even when the deprecated
    ``HEYSURE_DB_AUTO_MIGRATE`` switch is set, startup fails with an actionable
    error instead of attempting DDL and blocking normal database traffic.
    """
    from .core.settings import settings
    from . import db as _db

    if settings.db_auto_migrate:
        raise RuntimeError(
            "HEYSURE_DB_AUTO_MIGRATE is no longer supported by runtime services; "
            "run `python -m api.db migrate` as a separate deployment step and set it to 0"
        )
    has_version, has_core = _db._db_state(engine)
    if not (has_version and has_core):
        raise RuntimeError(
            "database schema is unversioned or incomplete; run `python -m api.db migrate` "
            "before starting runtime services"
        )

    expected = _db.expected_schema_revisions()
    current = _db.current_schema_revisions(engine)
    if current != expected:
        raise RuntimeError(
            "database schema revision mismatch: "
            f"current={sorted(current)} expected={sorted(expected)}; "
            "run `python -m api.db migrate` before starting runtime services"
        )

    _require_assistant_avatar_column(engine)
    logger.info("runtime schema guard passed (read-only, revision=%s)", sorted(current))


def get_session() -> Iterator[Session]:
    """FastAPI dependency with an explicit rollback boundary."""
    with Session(engine) as session:
        try:
            yield session
        except BaseException:
            session.rollback()
            raise
