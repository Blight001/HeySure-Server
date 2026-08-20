from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

from other.scripts import backup_postgres


def test_find_pg_dump_prefers_path(monkeypatch) -> None:
    monkeypatch.setattr(backup_postgres.shutil, "which", lambda name: "/tools/pg_dump")
    assert backup_postgres.find_pg_dump() == "/tools/pg_dump"


def test_backup_uses_password_environment_not_command_line(monkeypatch, tmp_path: Path) -> None:
    captured = {}
    target = tmp_path / "local-pre-migrate.dump"
    monkeypatch.setattr(backup_postgres, "find_pg_dump", lambda: "/tools/pg_dump")
    monkeypatch.setattr(backup_postgres, "SERVER_DIR", str(tmp_path))
    monkeypatch.setattr(
        backup_postgres,
        "settings",
        SimpleNamespace(database_url="postgresql+psycopg://user:secret@db.example/app"),
    )

    def fake_run(command, **kwargs):
        captured["command"] = command
        captured["env"] = kwargs["env"]
        output = Path(command[command.index("--file") + 1])
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_bytes(b"backup")
        return SimpleNamespace(returncode=0, stderr="")

    monkeypatch.setattr(backup_postgres.subprocess, "run", fake_run)
    path, size = backup_postgres.backup_postgres()

    assert path != target
    assert size == 6
    assert "secret" not in captured["command"]
    assert captured["env"]["PGPASSWORD"] == "secret"
