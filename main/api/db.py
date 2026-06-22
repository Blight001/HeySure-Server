"""Database schema lifecycle — Alembic is the single source of schema truth.

This module replaces the old "create_all + run_pending_migrations +
run_data_consolidations on every boot" path (see
``doc/enterprise-optimization.md`` §4). Alembic now owns the schema; this
module only orchestrates *applying* it and is safe to run as a standalone
step (CI / deploy Job / container entrypoint) decoupled from the app:

    python -m api.db migrate     # bring the configured DB to head (default)
    python -m api.db current     # print the DB's current revision
    python -m api.db stamp head  # mark a DB as being at a revision (no DDL)
    python -m api.db upgrade head
    python -m api.db downgrade -1
    python -m api.db check        # fail if models drift from migrations

Adoption strategy (``ensure_schema``) handles the three states a database can
be in, so a single command works for new installs and existing deployments:

- **empty DB** (no app tables)        -> ``alembic upgrade head`` builds the
  schema from the baseline (byte-for-byte identical to the old ``create_all``).
- **pre-Alembic DB** (app tables exist, no ``alembic_version``) -> run the
  one-time legacy catch-up (``api.core.migrations``) to bring schema + data
  current the exact way the old boot did, then ``stamp head`` to hand
  ownership to Alembic. This runs *at most once* per database.
- **Alembic-managed DB** (``alembic_version`` present) -> ``alembic upgrade
  head`` applies any new revisions (a fast no-op when already current).

The legacy catch-up runs *outside* any Alembic transaction so its own
``engine.begin()`` connections do not conflict with Alembic's transaction.
"""

import logging
import os
import sys

from alembic import command
from alembic.config import Config
from sqlalchemy import inspect

from .core.config import SERVER_DIR

logger = logging.getLogger(__name__)

_ALEMBIC_INI = os.path.join(SERVER_DIR, "alembic.ini")
_MIGRATIONS_DIR = os.path.join(SERVER_DIR, "other", "migrations")
# Presence of this table => the application schema has already been materialized
# (by a previous Alembic run or the legacy create_all path).
_CORE_TABLE = "user"


def alembic_config() -> Config:
    """Build an Alembic ``Config`` pinned to this project's migrations.

    ``env.py`` resolves the database URL from ``api.core.settings`` (or the
    ``ALEMBIC_DATABASE_URL`` override), so we do not set it here.
    """
    cfg = Config(_ALEMBIC_INI)
    cfg.set_main_option("script_location", _MIGRATIONS_DIR)
    return cfg


def _db_state(engine) -> tuple[bool, bool]:
    """Return ``(has_alembic_version, has_core_tables)`` for the given engine."""
    tables = set(inspect(engine).get_table_names())
    return ("alembic_version" in tables, _CORE_TABLE in tables)


def _legacy_adopt(engine, cfg: Config) -> None:
    """One-time adoption of a pre-Alembic database.

    Brings schema + data fully current using the proven legacy code, then
    stamps the DB at ``head`` so Alembic owns it from here on. Idempotent:
    a live deployment has already applied these on previous boots, so this is
    effectively a no-op beyond writing ``alembic_version``.
    """
    from .core import migrations as legacy

    logger.info("adopting pre-Alembic database: running one-time legacy catch-up")
    legacy.run_pending_migrations()
    legacy.run_data_consolidations(engine)
    command.stamp(cfg, "head")
    logger.info("legacy database adopted and stamped at Alembic head")


def ensure_schema() -> None:
    """Bring the configured database to the latest schema.

    Serialized across concurrently starting processes via the shared bootstrap
    lock so the four runtimes do not race on first boot.
    """
    # Imported here (not at module top) to avoid a circular import: database.py
    # imports nothing from this module at import time.
    from .database import _bootstrap_lock, engine

    with _bootstrap_lock():
        has_version, has_core = _db_state(engine)
        cfg = alembic_config()
        if not has_version and has_core:
            _legacy_adopt(engine, cfg)
            return
        # Empty DB -> baseline creates everything; managed DB -> apply new revisions.
        command.upgrade(cfg, "head")


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #

def _main(argv: list[str]) -> int:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    cmd = (argv[0] if argv else "migrate").strip()
    rest = argv[1:]
    cfg = alembic_config()

    if cmd == "migrate":
        ensure_schema()
        return 0
    if cmd == "current":
        command.current(cfg, verbose=True)
        return 0
    if cmd == "history":
        command.history(cfg, verbose=True)
        return 0
    if cmd == "upgrade":
        command.upgrade(cfg, rest[0] if rest else "head")
        return 0
    if cmd == "downgrade":
        if not rest:
            print("usage: python -m api.db downgrade <revision|-N|base>", file=sys.stderr)
            return 2
        command.downgrade(cfg, rest[0])
        return 0
    if cmd == "stamp":
        command.stamp(cfg, rest[0] if rest else "head")
        return 0
    if cmd == "check":
        command.check(cfg)
        return 0

    print(
        "usage: python -m api.db {migrate|current|history|upgrade|downgrade|stamp|check}",
        file=sys.stderr,
    )
    return 2


if __name__ == "__main__":
    raise SystemExit(_main(sys.argv[1:]))
