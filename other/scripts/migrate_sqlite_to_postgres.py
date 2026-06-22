"""One-shot SQLite -> Postgres data migration.

Usage:
    DATABASE_URL=postgresql://user:pass@host:5432/heysure \
    SOURCE_SQLITE=/abs/path/to/heysure.db \
    python -m scripts.migrate_sqlite_to_postgres

Behavior:
1. Connects to the source SQLite file (defaults to ``data/heysure.db``
   alongside the server package).
2. Runs the existing SQLite ALTER TABLE migrations against the source so
   the source schema is fully up to date before reading it.
3. Builds the destination Postgres schema with Alembic
   (``alembic upgrade head`` — the single source of schema truth), then
   copies rows table-by-table.
4. Resets Postgres sequences for tables with a serial primary key so that
   subsequent INSERTs do not collide with imported IDs.

Safe to re-run only against an empty Postgres database — by default the
script refuses to write into a table that already has rows. Pass
``--truncate`` to truncate destination tables first.
"""

from __future__ import annotations

import argparse
import os
import sys
import sqlite3
from typing import Any

# Make ``api.*`` imports resolve when this file runs as a module.
THIS_DIR = os.path.dirname(os.path.abspath(__file__))
SERVER_DIR = os.path.dirname(os.path.dirname(THIS_DIR))
for _path in (
    SERVER_DIR,
    os.path.join(SERVER_DIR, "main"),
    os.path.join(SERVER_DIR, "other"),
):
    if _path not in sys.path:
        sys.path.insert(0, _path)

from sqlalchemy import Boolean, create_engine as sa_create_engine, text  # noqa: E402
from sqlalchemy.engine import Engine  # noqa: E402
from sqlmodel import SQLModel  # noqa: E402

# Importing api.models populates SQLModel.metadata for create_all().
from api import models  # noqa: F401,E402
from api.core.config import SQLITE_FILE  # noqa: E402


def _source_sqlite_path() -> str:
    return os.environ.get("SOURCE_SQLITE", SQLITE_FILE)


def _destination_url() -> str:
    url = os.environ.get("DATABASE_URL", "").strip()
    if not url:
        sys.exit("DATABASE_URL not set; cannot migrate without a destination.")
    if not url.lower().startswith("postgres"):
        sys.exit(f"DATABASE_URL must be a Postgres URL, got: {url}")
    return url


def _table_names_in_metadata() -> list[str]:
    # Preserve metadata order so foreign-key dependencies are inserted first.
    return [t.name for t in SQLModel.metadata.sorted_tables]


def _metadata_columns(table: str) -> list[str]:
    sa_table = SQLModel.metadata.tables.get(table)
    if sa_table is None:
        return []
    return [column.name for column in sa_table.columns]


def _sqlite_columns(conn: sqlite3.Connection, table: str) -> list[str]:
    cursor = conn.execute(f"PRAGMA table_info({table})")
    return [row[1] for row in cursor.fetchall()]


def _shared_columns(src_cols: list[str], dest_cols: list[str]) -> list[str]:
    """Return columns present in both source and destination, in destination order."""
    src_set = set(src_cols)
    return [col for col in dest_cols if col in src_set]


def _coerce_for_destination(table: str, column: str, value: Any) -> Any:
    sa_table = SQLModel.metadata.tables.get(table)
    if sa_table is None or column not in sa_table.c:
        return value
    dest_column = sa_table.c[column]
    if isinstance(dest_column.type, Boolean):
        if value is None:
            return None
        return bool(value)
    return value


def _copy_table(
    src: sqlite3.Connection,
    dest: Engine,
    table: str,
    *,
    truncate: bool,
    batch_size: int = 500,
) -> tuple[int, str]:
    src_cols = _sqlite_columns(src, table)
    if not src_cols:
        return (0, "source-missing")
    dest_cols = _metadata_columns(table)
    if not dest_cols:
        return (0, "dest-missing")
    insert_cols = _shared_columns(src_cols, dest_cols)
    if not insert_cols:
        return (0, "no-shared-columns")

    with dest.begin() as conn:
        existing_count = conn.execute(text(f'SELECT COUNT(*) FROM "{table}"')).scalar() or 0
        if existing_count and not truncate:
            return (0, f"dest-not-empty({existing_count})")
        if existing_count and truncate:
            conn.execute(text(f'TRUNCATE TABLE "{table}" RESTART IDENTITY CASCADE'))

    cursor = src.execute(f"SELECT {', '.join(insert_cols)} FROM {table}")
    rows: list[dict[str, Any]] = []
    total = 0
    placeholders = ", ".join([f":{c}" for c in insert_cols])
    cols_sql = ", ".join([f'"{c}"' for c in insert_cols])
    insert_sql = text(f'INSERT INTO "{table}" ({cols_sql}) VALUES ({placeholders})')

    while True:
        batch = cursor.fetchmany(batch_size)
        if not batch:
            break
        rows = [
            {
                column: _coerce_for_destination(table, column, value)
                for column, value in zip(insert_cols, row)
            }
            for row in batch
        ]
        with dest.begin() as conn:
            conn.execute(insert_sql, rows)
        total += len(rows)

    return (total, "copied")


def _reset_sequences(dest: Engine) -> None:
    """Re-align Postgres serial sequences after manual ID inserts.

    SQLModel uses ``id INTEGER PRIMARY KEY`` which Postgres realizes as
    ``id SERIAL``. Inserting explicit IDs does not advance the sequence,
    so the next ORM insert would collide. Fix by setting each sequence to
    ``MAX(id)+1`` for every table that has an ``id`` column.
    """
    with dest.begin() as conn:
        for table in _table_names_in_metadata():
            try:
                conn.execute(
                    text(
                        "SELECT setval("
                        "pg_get_serial_sequence(:t, 'id'), "
                        "COALESCE((SELECT MAX(id) FROM \"" + table + "\"), 1), true)"
                    ),
                    {"t": table},
                )
            except Exception:
                # Tables without an ``id`` serial silently skip.
                pass


def main() -> int:
    parser = argparse.ArgumentParser(description="Migrate SQLite -> Postgres")
    parser.add_argument(
        "--truncate",
        action="store_true",
        help="Truncate destination tables before insert (use with care)",
    )
    args = parser.parse_args()

    src_path = _source_sqlite_path()
    if not os.path.exists(src_path):
        sys.exit(f"Source SQLite file not found: {src_path}")

    dest_url = _destination_url()

    print(f"[migrate] source: {src_path}")
    print(f"[migrate] destination: {dest_url}")

    # Run SQLite ALTER TABLE patches against the source first so its schema
    # matches the current models before we read it.
    from api.core import migrations as sqlite_migrations
    # Temporarily point migrations at the source file via env override.
    original_sqlite_file = sqlite_migrations.SQLITE_FILE
    try:
        sqlite_migrations.SQLITE_FILE = src_path
        sqlite_migrations.run_pending_migrations()
    finally:
        sqlite_migrations.SQLITE_FILE = original_sqlite_file

    src = sqlite3.connect(src_path)
    src.row_factory = sqlite3.Row

    # Build the destination schema via Alembic (the single source of schema
    # truth) instead of create_all, so the migrated database starts already
    # under version control. env.py honors ALEMBIC_DATABASE_URL.
    from alembic import command
    from api.db import alembic_config

    os.environ["ALEMBIC_DATABASE_URL"] = dest_url
    command.upgrade(alembic_config(), "head")

    dest = sa_create_engine(dest_url, pool_pre_ping=True)

    total_rows = 0
    skipped: list[tuple[str, str]] = []
    for table in _table_names_in_metadata():
        try:
            count, status = _copy_table(src, dest, table, truncate=args.truncate)
        except Exception as exc:
            print(f"[migrate] {table}: FAILED -> {exc}")
            skipped.append((table, f"error:{exc}"))
            continue
        if status == "copied":
            print(f"[migrate] {table}: {count} rows")
            total_rows += count
        else:
            print(f"[migrate] {table}: skipped ({status})")
            skipped.append((table, status))

    _reset_sequences(dest)

    print(f"[migrate] total rows copied: {total_rows}")
    if skipped:
        print("[migrate] skipped tables:")
        for name, reason in skipped:
            print(f"  - {name}: {reason}")

    src.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
