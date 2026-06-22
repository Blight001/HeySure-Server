"""Alembic environment.

Wires Alembic to the project's single source of schema truth:

- ``SQLModel.metadata`` — populated by importing :mod:`api.models` (the same
  metadata the application uses), so ``--autogenerate`` diffs against the live
  models.
- ``settings.database_url`` — the same PostgreSQL URL the app connects with.
  ``ALEMBIC_DATABASE_URL`` can override it for
  one-off targets (e.g. CI's throwaway databases).
"""

import os
import sys
from logging.config import fileConfig

from sqlalchemy import engine_from_config, pool
from sqlmodel import SQLModel

from alembic import context

# Make runtime packages importable when Alembic is invoked from anywhere.
_SERVER_DIR = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
for _path in (
    _SERVER_DIR,
    os.path.join(_SERVER_DIR, "main"),
    os.path.join(_SERVER_DIR, "other"),
):
    if _path not in sys.path:
        sys.path.insert(0, _path)

# Let standalone Alembic commands use their explicit override without also
# requiring a duplicate DATABASE_URL environment variable.
_override_db_url = os.environ.get("ALEMBIC_DATABASE_URL")
if _override_db_url:
    os.environ.setdefault("DATABASE_URL", _override_db_url)

# Importing the models package populates ``SQLModel.metadata`` as a side effect.
from api import models  # noqa: E402,F401
from api.core.settings import settings  # noqa: E402
from api.core.logging_config import is_configured as _app_logging_configured  # noqa: E402

config = context.config

# Only let Alembic own logging when running standalone (the ``alembic`` CLI).
# When migrations run in-process during app startup, the app has already
# configured logging; ``fileConfig`` would disable every existing logger
# (uvicorn.access / uvicorn.error / gateway.app …) and replace the root
# handlers, silencing ALL logs — including the HTTP access log — for the rest
# of the process. ``disable_existing_loggers=False`` is belt-and-braces for the
# standalone path.
if config.config_file_name is not None and not _app_logging_configured():
    fileConfig(config.config_file_name, disable_existing_loggers=False)

# Resolve the database URL: explicit override > app settings.
_db_url = _override_db_url or settings.database_url
if not _db_url.lower().startswith(("postgresql://", "postgresql+psycopg://")):
    raise RuntimeError("Alembic requires a PostgreSQL DATABASE_URL")
config.set_main_option("sqlalchemy.url", _db_url)

target_metadata = SQLModel.metadata


def run_migrations_offline() -> None:
    """Run migrations in 'offline' mode (emit SQL without a DBAPI connection)."""
    context.configure(
        url=_db_url,
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        compare_type=True,
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    """Run migrations in 'online' mode (against a live connection)."""
    connectable = engine_from_config(
        config.get_section(config.config_ini_section, {}),
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )
    with connectable.connect() as connection:
        context.configure(
            connection=connection,
            target_metadata=target_metadata,
            compare_type=True,
        )
        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
