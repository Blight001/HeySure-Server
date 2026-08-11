from other.scripts import rolling_release


def test_release_builds_migration_image_before_running_migration(monkeypatch):
    calls = []
    revisions = iter(["old", "new"])

    monkeypatch.setattr(rolling_release, "compose", lambda *args, **kwargs: calls.append(args) or "")
    monkeypatch.setattr(rolling_release, "database_revision", lambda: next(revisions))
    monkeypatch.setattr(rolling_release, "previous_image", lambda _service: None)
    monkeypatch.setattr(rolling_release, "wait_ready", lambda *_args: None)

    rolling_release.release(10)

    build_index = next(i for i, call in enumerate(calls) if call[0] == "build")
    migrate_index = calls.index(("run", "--rm", "db-migrate"))
    assert "db-migrate" in calls[build_index]
    assert build_index < migrate_index


def test_release_does_not_restore_old_image_after_revision_advance(monkeypatch):
    revisions = iter(["old", "new"])
    rollbacks = []

    monkeypatch.setattr(rolling_release, "compose", lambda *_args, **_kwargs: "")
    monkeypatch.setattr(rolling_release, "database_revision", lambda: next(revisions))
    monkeypatch.setattr(rolling_release, "previous_image", lambda _service: None)
    monkeypatch.setattr(rolling_release, "wait_ready", lambda *_args: (_ for _ in ()).throw(RuntimeError("no")))
    monkeypatch.setattr(rolling_release, "rollback", lambda *_args: rollbacks.append(True))

    try:
        rolling_release.release(10)
    except RuntimeError:
        pass

    assert rollbacks == []
