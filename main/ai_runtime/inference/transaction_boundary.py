"""Keep database transactions out of slow external I/O waits."""

from sqlmodel import Session


def release_clean_session_before_external_io(
    session: Session,
    *,
    boundary: str,
) -> None:
    """End an autobegun read transaction before model/tool network I/O.

    SQLAlchemy starts a transaction for ordinary SELECTs.  The inference
    worker keeps one Session across a run, so a read performed immediately
    before a slow model or MCP call would otherwise remain ``idle in
    transaction`` until PostgreSQL terminates it.  Never hide pending writes:
    callers must persist them explicitly before crossing this boundary.
    """
    in_transaction = getattr(session, "in_transaction", None)
    if not callable(in_transaction) or not in_transaction():
        return
    pending = _pending_change_kinds(session)
    if pending:
        kinds = ", ".join(pending)
        raise RuntimeError(
            f"cannot start {boundary} with pending database changes ({kinds})"
        )
    session.rollback()


def _pending_change_kinds(session: Session) -> list[str]:
    kinds = []
    if getattr(session, "new", None):
        kinds.append("new")
    if _has_modified_rows(session):
        kinds.append("dirty")
    if getattr(session, "deleted", None):
        kinds.append("deleted")
    return kinds


def _has_modified_rows(session: Session) -> bool:
    dirty = tuple(getattr(session, "dirty", ()) or ())
    if not dirty:
        return False
    is_modified = getattr(session, "is_modified", None)
    if not callable(is_modified):
        return True
    return any(is_modified(row, include_collections=True) for row in dirty)
