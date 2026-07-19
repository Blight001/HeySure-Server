"""``ai-runtime`` worker pool — consume chat runs from the Postgres queue.

A single LISTEN connection waits on the ``ai_run_queued``
  channel. Each NOTIFY wakes the dispatcher, which then claims one row via
  ``SELECT … FROM chatrun WHERE status='queued' ORDER BY id FOR UPDATE
  SKIP LOCKED LIMIT 1``. Multiple ai-runtime instances can run side by
  side and the database arbitrates fairly.

The actual heavy lifting is delegated to ``_run_worker`` in a dedicated
thread per claimed run, and the dispatcher loops back for the next NOTIFY.
"""

from __future__ import annotations

import logging
import threading
import time
import traceback
from typing import Any, Dict, Optional

from sqlmodel import Session, select

from api.core.settings import settings
from api.database import engine
from api.models import ChatRun
from api.runtime import heartbeat


logger = logging.getLogger(__name__)


DATABASE_URL = settings.database_url


def psycopg_dsn() -> str:
    return settings.psycopg_dsn


QUEUE_CHANNEL = "ai_run_queued"
# Cap on simultaneous in-flight runs per ai-runtime instance. Each run holds
# a thread + Python stack + an LLM HTTP connection, so unbounded growth from
# a NOTIFY burst would exhaust the process. Override via env if you scale
# vertically.
_MAX_CONCURRENT_RUNS = max(1, settings.ai_runtime_max_concurrent)
_run_slots = threading.Semaphore(_MAX_CONCURRENT_RUNS)

# Live in-flight run bookkeeping so the ai-runtime status server can report
# "what is this worker doing right now" to the admin panel.
_active_runs_lock = threading.Lock()
_active_run_ids: set[str] = set()


def active_runs() -> Dict[str, Any]:
    """Snapshot of this worker's concurrency for the admin panel."""
    with _active_runs_lock:
        run_ids = sorted(_active_run_ids)
    return {
        "active": len(run_ids),
        "max_concurrent": _MAX_CONCURRENT_RUNS,
        "run_ids": run_ids,
    }


def notify_queue(run_id: str) -> None:
    """Best-effort Postgres NOTIFY after a queued run is committed.

    Callers must have already INSERTed the queued ChatRun row + committed.
    The payload carries the ``run_id`` so the dispatcher could (in theory)
    skip the SKIP LOCKED query, but we keep that path for fairness with
    multiple workers.
    """
    try:
        import psycopg
    except Exception:
        return
    try:
        # psycopg does not understand SQLAlchemy's ``postgresql+psycopg://``
        # form, so use a normalized libpq DSN here.
        with psycopg.connect(psycopg_dsn(), autocommit=True) as conn:
            # Channel/payload identifiers are constants; payload is the
            # run_id which we sanitize defensively.
            safe_payload = str(run_id or "").replace("'", "")
            conn.execute(f"NOTIFY {QUEUE_CHANNEL}, '{safe_payload}'")
    except Exception:
        logger.exception(f"NOTIFY failed for {run_id}")


def _claim_one_queued_run() -> Optional[ChatRun]:
    """Atomically claim one queued run; mark it running with a heartbeat."""
    with Session(engine) as session:
        from sqlalchemy import text

        row_id = session.exec(
            text(
                "SELECT id FROM chatrun WHERE status = 'queued' "
                "ORDER BY id FOR UPDATE SKIP LOCKED LIMIT 1"
            )
        ).scalar_one_or_none()
        if row_id is None:
            return None
        run = session.get(ChatRun, int(row_id))
        if not run:
            return None
        now = time.time()
        run.status = "running"
        run.started_at = run.started_at or now
        run.heartbeat_at = now
        run.updated_at = now
        session.add(run)
        session.commit()
        session.refresh(run)
        return run


def _load_worker_kwargs(run: ChatRun) -> Dict[str, Any]:
    """Re-derive the kwargs needed by ``_run_worker`` from a ChatRun row.

    Reads ``worker_kwargs_json`` for caller-provided overrides
    (``merged_system_prompt`` built with Feishu runtime guidance,
    custom ``max_steps``, etc.). Anything missing from the JSON blob is
    rebuilt from the chat session + AI config defaults so older rows or
    callers that didn't persist extras still work.
    """
    import json as _json

    from api.chat_runtime.chat_runtime_helpers import _resolve_ai_runtime
    from api.models import ChatMessage, User

    extras: Dict[str, Any] = {}
    if run.worker_kwargs_json:
        try:
            parsed = _json.loads(run.worker_kwargs_json)
            if isinstance(parsed, dict):
                extras = parsed
        except Exception:
            extras = {}

    with Session(engine) as session:
        user = session.get(User, run.user_id)
        if not user:
            raise RuntimeError(f"user {run.user_id} not found for run {run.run_id}")
        _, _, _, _, default_system_prompt = _resolve_ai_runtime(
            session, user, run.ai_kind, run.ai_config_id, run.session_id
        )
        last_user_msg = session.exec(
            select(ChatMessage)
            .where(
                ChatMessage.user_id == run.user_id,
                ChatMessage.session_id == run.session_id,
                ChatMessage.ai_kind == run.ai_kind,
                ChatMessage.role == "user",
            )
            .order_by(ChatMessage.created_at.desc())
        ).first()

    return {
        "run_id": run.run_id,
        "user_id": run.user_id,
        "ai_config_id": run.ai_config_id,
        "ai_kind": run.ai_kind,
        "session_id": run.session_id,
        "session_name": run.session_name or "",
        "model_user_content": extras.get("model_user_content")
            or (last_user_msg.content if last_user_msg else ""),
        "merged_system_prompt": extras.get("merged_system_prompt") or default_system_prompt,
        "max_steps": extras.get("max_steps"),
        "current_user_message_id": extras.get("current_user_message_id")
            or (last_user_msg.id if last_user_msg else None),
    }


def _execute_run(run: ChatRun) -> None:
    # ``_run_worker`` now spawns its own heartbeat thread, so the dispatcher
    # just needs to materialize the kwargs and call it.
    from ai_runtime.inference.core import _run_worker

    try:
        kwargs = _load_worker_kwargs(run)
    except Exception as exc:
        traceback.print_exc()
        # Mark as error so the UI moves on.
        with Session(engine) as session:
            row = session.get(ChatRun, run.id)
            if row:
                row.status = "error"
                row.error_message = f"worker setup failed: {exc}"
                row.finished_at = time.time()
                row.updated_at = time.time()
                session.add(row)
                session.commit()
        return

    try:
        _run_worker(**kwargs)
    except Exception as exc:
        traceback.print_exc()
        with Session(engine) as session:
            row = session.get(ChatRun, run.id)
            if row and row.status not in {"completed", "stopped", "error"}:
                row.status = "error"
                row.error_message = f"_run_worker crashed: {exc}"
                row.finished_at = time.time()
                row.updated_at = time.time()
                session.add(row)
                session.commit()


def _execute_run_with_slot(run: ChatRun) -> None:
    with _active_runs_lock:
        _active_run_ids.add(run.run_id)
    try:
        _execute_run(run)
    finally:
        with _active_runs_lock:
            _active_run_ids.discard(run.run_id)
        _run_slots.release()


def _drain_dispatcher(limit: int = 32) -> int:
    """Claim and dispatch as many queued runs as we can find in one sweep,
    bounded by the concurrency semaphore so a burst of NOTIFYs cannot
    spawn more worker threads than the process can sustain.
    """
    dispatched = 0
    while dispatched < limit:
        # Non-blocking acquire — if we've hit the cap, leave the rest in
        # the queue. The next NOTIFY (or the next poll tick) will pick them
        # up when a slot frees up.
        if not _run_slots.acquire(blocking=False):
            return dispatched
        run = _claim_one_queued_run()
        if not run:
            _run_slots.release()
            return dispatched
        th = threading.Thread(
            target=_execute_run_with_slot,
            args=(run,),
            name=f"runworker-{run.run_id}",
            daemon=True,
        )
        th.start()
        dispatched += 1
    return dispatched


def _retry_tick_loop(stop_evt: threading.Event) -> None:
    """Periodic safety-net dispatcher tick.

    Without this, a NOTIFY burst could leave queued rows stranded once the
    concurrency cap is reached: subsequent NOTIFYs wake us but already-claimed
    rows that finish freeing slots don't re-trigger drain. A cheap timer-based
    drain ensures backlogged rows are picked up within ``RETRY_TICK_SECONDS``.
    """
    RETRY_TICK_SECONDS = 10.0
    while not stop_evt.is_set():
        if stop_evt.wait(RETRY_TICK_SECONDS):
            return
        try:
            _drain_dispatcher()
        except Exception:
            logger.exception("retry-tick drain failed")


def _listen_postgres(stop_evt: threading.Event) -> None:
    import psycopg

    # Run the retry tick alongside LISTEN so slot-release backlogs aren't
    # stranded waiting for the next NOTIFY.
    retry_thread = threading.Thread(
        target=_retry_tick_loop, args=(stop_evt,), name="ai-runtime-retry-tick", daemon=True,
    )
    retry_thread.start()

    backoff = 1.0
    while not stop_evt.is_set():
        try:
            with psycopg.connect(psycopg_dsn(), autocommit=True) as conn:
                conn.execute(f"LISTEN {QUEUE_CHANNEL}")
                # Drain anything that landed before we started listening.
                _drain_dispatcher()
                backoff = 1.0
                while not stop_evt.is_set():
                    # Give the loop a bounded wait so Ctrl+C can unwind
                    # promptly instead of blocking forever on notifies().
                    for _notify in conn.notifies(timeout=1.0):
                        if stop_evt.is_set():
                            return
                        _drain_dispatcher()
        except Exception:
            logger.exception("listen loop failed, retrying")
            if stop_evt.wait(backoff):
                return
            backoff = min(backoff * 2, 30.0)


def run_dispatcher_forever(stop_evt: Optional[threading.Event] = None) -> None:
    """Block forever, dispatching queued runs as they appear.

    Designed to be called from ``ai_runtime/main.py``.
    """
    evt = stop_evt or threading.Event()
    logger.info(
        f"dispatcher starting (dialect=postgresql, db={DATABASE_URL.split('@')[-1]})"
    )
    # Boot-time recovery sweep — re-enqueue any 'running' rows whose worker
    # died across the restart. The watchdog will reap if heartbeats are
    # already stale, but on fresh boot we also retry orphans.
    try:
        reaped = heartbeat.reap_stale_runs()
        if reaped:
            logger.warning(f"boot watchdog reaped {len(reaped)} stale runs")
    except Exception:
        logger.exception("boot reap failed")
    _listen_postgres(evt)
