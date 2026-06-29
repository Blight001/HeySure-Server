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
    """Serialize schema/bootstrap work across concurrently starting services."""
    deadline = time.time() + 120.0
    with engine.connect() as conn:
        while True:
            locked = conn.exec_driver_sql(
                f"SELECT pg_try_advisory_lock({_BOOTSTRAP_ADVISORY_LOCK_KEY})"
            ).scalar()
            if locked:
                try:
                    yield
                finally:
                    try:
                        conn.exec_driver_sql(
                            f"SELECT pg_advisory_unlock({_BOOTSTRAP_ADVISORY_LOCK_KEY})"
                        )
                    except Exception:
                        logger.exception("failed to release postgres bootstrap lock")
                return
            if time.time() >= deadline:
                raise RuntimeError(
                    "database is busy; another process is still bootstrapping the Postgres database"
                )
            time.sleep(0.5)


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

    if settings.db_auto_migrate:
        _db.ensure_schema()
        return

    has_version, has_core = _db._db_state(engine)
    if not (has_version or has_core):
        raise RuntimeError(
            "database schema is not initialized and HEYSURE_DB_AUTO_MIGRATE is off; "
            "run `python -m api.db migrate` before starting the app"
        )


def get_session():
    with Session(engine) as session:
        yield session
