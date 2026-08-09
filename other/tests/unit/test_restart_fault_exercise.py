from __future__ import annotations

import pytest

from other.scripts import restart_fault_exercise as exercise


def test_smoke_passes_explicit_existing_account(monkeypatch) -> None:
    captured = []
    monkeypatch.setattr(exercise, "compose", lambda *args, **kwargs: captured.extend(args))

    exercise.smoke("internal-secret", 30.0, "heysure", "heysure")

    assert captured[captured.index("--account") + 1] == "heysure"
    assert captured[captured.index("--password") + 1] == "heysure"


def test_smoke_redacts_internal_token_from_failure(monkeypatch) -> None:
    def fail(*args, **kwargs):
        raise RuntimeError("command failed: --internal-token internal-secret")

    monkeypatch.setattr(exercise, "compose", fail)

    with pytest.raises(RuntimeError, match="^four-runtime smoke failed$") as error:
        exercise.smoke("internal-secret", 30.0, "heysure", "heysure")

    assert "internal-secret" not in str(error.value)
