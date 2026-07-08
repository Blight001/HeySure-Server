"""User-message mid-run injection queue.

When a user sends a message while a chat run is already in flight, we no longer
want to block until the whole run finishes. Instead the message is persisted as
an ordinary user ``ChatMessage`` carrying the :data:`PENDING_INJECT_TAG` tag.
The running worker polls this queue at every step boundary (after a deep-thinking
turn or an MCP call) and injects the message straight into the live model
conversation — so a follow-up lands within one step instead of after the entire
run.

DB-backed (not in-process) so it survives the api-gateway ↔ ai-runtime process
boundary in split deployments, mirroring ``ai_message_service.pop_pending_for``.

Consuming a pending message clears its tag, turning it into a normal user
message. That is what guarantees exactly-once injection: the live worker adds it
to ``convo`` via the drain, and any *future* run rebuild sees it as ordinary
history (never re-injected).
"""

from __future__ import annotations

import json
import threading
import uuid
from typing import List, Optional

from sqlmodel import Session, select

from api.database import engine
from api.models import ChatMessage, ChatMessageCreate, ChatRun
from api.services.chat.chat_persistence import _save_message
import logging


logger = logging.getLogger(__name__)


PENDING_INJECT_TAG = "pending_user_inject"


def _match_config(stmt, ai_config_id: Optional[int]):
    if ai_config_id is not None:
        return stmt.where(ChatMessage.ai_config_id == ai_config_id)
    return stmt.where(ChatMessage.ai_config_id.is_(None))


def _pending_stmt(user_id: int, ai_config_id: Optional[int], ai_kind: str, session_id: str):
    stmt = select(ChatMessage).where(
        ChatMessage.user_id == user_id,
        ChatMessage.ai_kind == ai_kind,
        ChatMessage.session_id == session_id,
        ChatMessage.role == "user",
        ChatMessage.tags == PENDING_INJECT_TAG,
    )
    return _match_config(stmt, ai_config_id)


def queue_pending_inject(
    session: Session,
    *,
    user_id: int,
    ai_config_id: Optional[int],
    ai_kind: str,
    session_id: str,
    session_name: str,
    content: str,
) -> ChatMessage:
    """Persist a user message to be injected into the active run mid-flight."""
    return _save_message(
        session,
        user_id,
        ChatMessageCreate(
            role="user",
            content=content,
            tags=PENDING_INJECT_TAG,
            ai_config_id=ai_config_id,
            ai_kind=ai_kind,
            session_id=session_id,
            session_name=session_name,
        ),
    )


def pop_pending_injects(
    user_id: int,
    ai_config_id: Optional[int],
    ai_kind: str,
    session_id: str,
) -> List[str]:
    """Atomically fetch every pending user-inject message for this
    ``(user, AI, kind, session)`` in arrival order, clear its pending tag, and
    return their contents. Clearing the tag demotes each row to an ordinary user
    message so it enters normal history exactly once and is never re-injected.
    """
    session_id = (session_id or "").strip()
    if not session_id:
        return []
    contents: List[str] = []
    with Session(engine) as session:
        rows = session.exec(
            _pending_stmt(user_id, ai_config_id, ai_kind, session_id).order_by(
                ChatMessage.created_at.asc()
            )
        ).all()
        for row in rows:
            text = str(row.content or "").strip()
            if text:
                contents.append(text)
            row.tags = ""
            session.add(row)
        if rows:
            session.commit()
    return contents


def has_pending_injects(
    user_id: int,
    ai_config_id: Optional[int],
    ai_kind: str,
    session_id: str,
) -> bool:
    session_id = (session_id or "").strip()
    if not session_id:
        return False
    with Session(engine) as session:
        return session.exec(
            _pending_stmt(user_id, ai_config_id, ai_kind, session_id)
        ).first() is not None


def find_live_active_run(
    session: Session,
    *,
    user_id: int,
    ai_config_id: Optional[int],
    ai_kind: str,
    session_id: str,
) -> Optional[ChatRun]:
    """Return an in-flight run for this session, if any.

    Status-based (``queued``/``running``) so it works across the api-gateway ↔
    ai-runtime process boundary, matching how ``/run/active`` decides liveness.
    """
    stmt = select(ChatRun).where(
        ChatRun.user_id == user_id,
        ChatRun.ai_kind == ai_kind,
        ChatRun.session_id == session_id,
        ChatRun.status.in_(["queued", "running"]),
    )
    if ai_config_id is not None:
        stmt = stmt.where(ChatRun.ai_config_id == ai_config_id)
    else:
        stmt = stmt.where(ChatRun.ai_config_id.is_(None))
    return session.exec(stmt.order_by(ChatRun.updated_at.desc())).first()


def resume_orphaned_injects(
    *,
    user_id: int,
    ai_config_id: Optional[int],
    ai_kind: str,
    session_id: str,
    session_name: str,
) -> Optional[str]:
    """Race backstop: if a run finished with user-injects still pending (the
    message landed after the worker's last drain but before it committed
    ``completed``), start a fresh continuation run to answer them.

    The pending tags are cleared first, so the messages become ordinary history
    and the new run responds to them through the standard path (no drain
    reliance) — which keeps it correct in both local and remote dispatch modes.
    Self-guards on "still pending" + "no live run", so it is safe (and cheap) to
    call from every worker's teardown and terminates naturally.
    """
    session_id = (session_id or "").strip()
    if not session_id:
        return None
    from api.core.settings import settings

    with Session(engine) as session:
        if find_live_active_run(
            session,
            user_id=user_id,
            ai_config_id=ai_config_id,
            ai_kind=ai_kind,
            session_id=session_id,
        ):
            return None
        rows = session.exec(
            _pending_stmt(user_id, ai_config_id, ai_kind, session_id).order_by(
                ChatMessage.created_at.asc()
            )
        ).all()
        if not rows:
            return None
        for row in rows:
            row.tags = ""
            session.add(row)
        run_id = f"run_{uuid.uuid4().hex}"
        session.add(
            ChatRun(
                run_id=run_id,
                user_id=user_id,
                ai_config_id=ai_config_id,
                ai_kind=ai_kind,
                session_id=session_id,
                session_name=session_name,
                status="queued",
                stop_requested=False,
            )
        )
        session.commit()

    if settings.ai_dispatch_mode == "remote":
        from ai_runtime.worker import notify_queue

        notify_queue(run_id)
        return run_id

    from api.chat_runtime.run_state import _RUN_THREADS
    from ai_runtime.inference.core import _run_worker

    worker = threading.Thread(
        target=_run_worker,
        kwargs={
            "run_id": run_id,
            "user_id": user_id,
            "ai_config_id": ai_config_id,
            "ai_kind": ai_kind,
            "session_id": session_id,
            "session_name": session_name,
            "model_user_content": None,
            "merged_system_prompt": None,
            "max_steps": None,
        },
        daemon=True,
    )
    _RUN_THREADS[run_id] = worker
    worker.start()
    return run_id
