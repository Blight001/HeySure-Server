"""Server-side revocation for user sessions and in-flight chat work."""

import time

from sqlmodel import Session, select

from api.models import ChatRun, User


def revoke_user_sessions(session: Session, user: User) -> list[str]:
    """Invalidate issued JWTs and stop chat runs owned by ``user``.

    The caller owns the transaction and must commit. Returning run IDs keeps
    this shared service free of Gateway-only live-state imports.
    """
    # Serialize concurrent logout/password-reset requests so a stale ORM value
    # cannot overwrite a newer version and accidentally preserve a token.
    locked_user = session.exec(
        select(User)
        .where(User.id == user.id)
        .with_for_update()
        .execution_options(populate_existing=True)
    ).one()
    locked_user.auth_version += 1
    session.add(locked_user)
    now = time.time()
    runs = session.exec(
        select(ChatRun).where(
            ChatRun.user_id == user.id,
            ChatRun.status.in_(["queued", "running"]),
        )
    ).all()
    for run in runs:
        run.stop_requested = True
        run.status = "stopped"
        run.updated_at = now
        if run.finished_at is None:
            run.finished_at = now
        session.add(run)
    return [str(run.run_id) for run in runs]
