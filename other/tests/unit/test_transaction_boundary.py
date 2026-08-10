from types import SimpleNamespace

import pytest

from ai_runtime.inference.transaction_boundary import (
    release_clean_session_before_external_io,
)


class FakeSession:
    def __init__(self, *, active=True, new=(), dirty=(), deleted=()):
        self.active = active
        self.new = tuple(new)
        self.dirty = tuple(dirty)
        self.deleted = tuple(deleted)
        self.rolled_back = False

    def in_transaction(self):
        return self.active

    def is_modified(self, row, *, include_collections):
        return bool(row.modified)

    def rollback(self):
        self.active = False
        self.rolled_back = True


def test_clean_autobegun_transaction_is_released():
    session = FakeSession()

    release_clean_session_before_external_io(session, boundary="MCP tool test")

    assert session.rolled_back is True
    assert session.active is False


def test_inactive_session_is_unchanged():
    session = FakeSession(active=False)

    release_clean_session_before_external_io(session, boundary="model request")

    assert session.rolled_back is False


@pytest.mark.parametrize(
    ("field", "value", "kind"),
    [
        ("new", (object(),), "new"),
        ("dirty", (SimpleNamespace(modified=True),), "dirty"),
        ("deleted", (object(),), "deleted"),
    ],
)
def test_pending_changes_are_never_silently_rolled_back(field, value, kind):
    session = FakeSession(**{field: value})

    with pytest.raises(RuntimeError, match=kind):
        release_clean_session_before_external_io(
            session,
            boundary="external wait",
        )

    assert session.rolled_back is False


def test_unmodified_dirty_identity_does_not_block_release():
    session = FakeSession(dirty=(SimpleNamespace(modified=False),))

    release_clean_session_before_external_io(session, boundary="model request")

    assert session.rolled_back is True
