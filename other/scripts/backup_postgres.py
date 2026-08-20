"""Create a secret-safe PostgreSQL custom-format backup for local releases."""

from __future__ import annotations

import argparse
import os
import shutil
import subprocess
from datetime import datetime
from pathlib import Path

from sqlalchemy.engine import make_url

from api.core.config import SERVER_DIR
from api.core.settings import settings
from api.database import engine
from api.db import current_schema_revisions, expected_schema_revisions


def find_pg_dump() -> str:
    discovered = shutil.which("pg_dump")
    if discovered:
        return discovered
    if os.name == "nt":
        root = Path(os.environ.get("ProgramFiles", r"C:\Program Files")) / "PostgreSQL"
        candidates = sorted(root.glob("*/bin/pg_dump.exe"), reverse=True)
        if candidates:
            return str(candidates[0])
    raise RuntimeError("pg_dump not found; install PostgreSQL client tools before migrating")


def backup_postgres() -> tuple[Path, int]:
    url = make_url(settings.database_url)
    backup_dir = Path(SERVER_DIR) / "data" / "backups"
    backup_dir.mkdir(parents=True, exist_ok=True)
    path = backup_dir / f"local-pre-migrate-{datetime.now():%Y%m%d-%H%M%S}.dump"
    env = os.environ.copy()
    env["PGPASSWORD"] = url.password or ""
    command = [
        find_pg_dump(), "--format=custom", "--no-owner", "--no-acl",
        "--file", str(path), "--host", url.host or "127.0.0.1",
        "--port", str(url.port or 5432), "--username", url.username or "",
        url.database or "",
    ]
    result = subprocess.run(command, env=env, capture_output=True, text=True, encoding="utf-8", errors="replace")
    if result.returncode:
        raise RuntimeError("pg_dump failed; database migration was not started")
    size = path.stat().st_size
    if size <= 0:
        raise RuntimeError("pg_dump produced an empty backup")
    return path, size


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--if-pending", action="store_true")
    args = parser.parse_args()
    if args.if_pending and current_schema_revisions(engine) == expected_schema_revisions():
        print("backup_not_needed=true")
        return 0
    path, size = backup_postgres()
    print(f"backup={path}")
    print(f"bytes={size}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
