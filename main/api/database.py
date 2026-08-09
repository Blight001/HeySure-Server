"""SQLModel engine + session dependency.

Schema is owned by Alembic (see ``api.db`` / ``other/migrations/`` /
``doc/db-migrations.md``). This module only builds the engine and exposes the
session dependency; bringing the schema up to date is delegated to
``api.db.ensure_schema`` via :func:`create_db_and_tables`.
"""

import contextlib
import logging
import time

from sqlmodel import Session, create_engine

from .core.config import DATABASE_URL

# Importing the models package side-effect populates ``SQLModel.metadata``.
from . import models  # noqa: F401

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


engine = create_engine(
    DATABASE_URL,
    pool_pre_ping=True,
    pool_recycle=300,
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
    """Ensure the database schema is current. Called by each runtime at startup.

    Backwards-compatible entry point. By default it runs Alembic
    ``upgrade head`` (adopting pre-Alembic databases on first boot). Set
    ``HEYSURE_DB_AUTO_MIGRATE=0`` to decouple migration from startup — run
    ``python -m api.db migrate`` as a separate deploy step instead, and the
    app will only verify the schema is present.
    """
    from .core.settings import settings
    from . import db as _db

    logger.info("create_db_and_tables starting (auto_migrate=%s)", settings.db_auto_migrate)
    if settings.db_auto_migrate:
        _db.ensure_schema()
        logger.info("create_db_and_tables completed via ensure_schema")
    else:
        has_version, has_core = _db._db_state(engine)
        if not (has_version or has_core):
            raise RuntimeError(
                "database schema is not initialized and HEYSURE_DB_AUTO_MIGRATE is off; "
                "run `python -m api.db migrate` before starting the app"
            )
        logger.info("create_db_and_tables completed (no auto migrate)")

    _require_assistant_avatar_column(engine)
    logger.info("create_db_and_tables: verified 'avatar' column")


def get_session():
    with Session(engine) as session:
        yield session
