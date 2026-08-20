from __future__ import annotations

from types import SimpleNamespace

import tk_launcher


def test_launcher_lists_every_current_service_port() -> None:
    assert {spec.port for spec in tk_launcher.SERVICES} == {
        "3000", "3001", "3002", "3003", "58150", "58151", "58152",
    }


def test_schema_migration_runs_once_per_launcher_batch(monkeypatch) -> None:
    calls = []
    monkeypatch.setattr(
        tk_launcher.subprocess,
        "run",
        lambda *args, **kwargs: calls.append((args, kwargs)) or SimpleNamespace(
            returncode=0, stdout="migration complete", stderr="",
        ),
    )
    tk_launcher.reset_schema_migration_state()

    first = tk_launcher.ensure_schema_current({"PYTHONPATH": "main;."})
    second = tk_launcher.ensure_schema_current({"PYTHONPATH": "main;."})

    assert first[0] is True
    assert second[0] is True
    assert len(calls) == 2
    assert calls[0][0][0][-3:] == ["-m", "other.scripts.backup_postgres", "--if-pending"]
    assert calls[1][0][0][-3:] == ["-m", "api.db", "migrate"]


def test_schema_migration_failure_blocks_runtime_start(monkeypatch) -> None:
    monkeypatch.setattr(
        tk_launcher.subprocess,
        "run",
        lambda *args, **kwargs: SimpleNamespace(returncode=1, stdout="", stderr="backup rejected"),
    )
    tk_launcher.reset_schema_migration_state()

    ok, detail = tk_launcher.ensure_schema_current({})

    assert ok is False
    assert "数据库备份失败" in detail
