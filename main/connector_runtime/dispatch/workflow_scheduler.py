"""Connector-only workflow compensation and advancement loop."""

from __future__ import annotations

import asyncio
import logging
import socket
import time
import uuid

from sqlmodel import Session, select

from api.core.settings import settings
from api.database import engine
from api.models import WorkflowRun, WorkflowSchedulerHeartbeat
from api.services.workflows.run_service import error_payload, fail_run, run_payload
from api.services.workflows.run_service import expire_confirmations
from api.services.workflows.ai_interaction import (
    advance_interactive_run,
    expire_ai_interactions,
)
from api.services.workflows.ai_interaction_notifier import process_pending_ai_interactions
from connector_runtime.dispatch.workflow_dispatch import (
    dispatch_pending_steps,
    reconcile_finished_dispatches,
)


logger = logging.getLogger(__name__)
SCHEDULER_INSTANCE_ID = f"{socket.gethostname()}-{uuid.uuid4().hex[:12]}"


def _record_heartbeat(started_at: float, error: str = "") -> None:
    with Session(engine) as session:
        row = session.get(WorkflowSchedulerHeartbeat, SCHEDULER_INSTANCE_ID)
        if not row:
            row = WorkflowSchedulerHeartbeat(instance_id=SCHEDULER_INSTANCE_ID)
        row.heartbeat_at = time.time()
        row.last_tick_duration_ms = int(max(0.0, row.heartbeat_at - started_at) * 1000)
        row.last_error = str(error or "")[:2000]
        session.add(row)
        session.commit()


async def _emit_updated_runs(since: float) -> None:
    from api.sio import sio

    with Session(engine) as session:
        rows = session.exec(
            select(WorkflowRun).where(WorkflowRun.updated_at >= since).limit(200)
        ).all()
    for row in rows:
        await sio.emit("workflow:run_update", run_payload(row), room=f"user_{row.user_id}")


async def _emit_confirmation_updates(since: float) -> None:
    from api.sio import sio
    from api.services.workflows.confirmation_notifications import notification_events_since, notification_room

    with Session(engine) as session:
        requested, resolved = notification_events_since(session, since=since)
    for payload in requested:
        await sio.emit(
            "workflow:confirmation_requested",
            payload,
            room=notification_room(payload["requested_user_id"]),
        )
    for payload in resolved:
        await sio.emit(
            "workflow:confirmation_resolved",
            payload,
            room=notification_room(payload["requested_user_id"]),
        )


def _advance_ready_runs(limit: int = 100) -> int:
    now = time.time()
    with Session(engine) as session:
        rows = session.exec(
            select(WorkflowRun)
            .where(
                WorkflowRun.status.in_(["pending", "running", "retry_wait"]),
                WorkflowRun.next_wakeup_at <= now,
            )
            .order_by(WorkflowRun.next_wakeup_at, WorkflowRun.created_at)
            .limit(limit)
        ).all()
    count = 0
    for row in rows:
        with Session(engine) as session:
            advance_interactive_run(session, row.id)
            count += 1
    return count


def _expire_waiting_runs(limit: int = 100) -> int:
    now = time.time()
    with Session(engine) as session:
        rows = session.exec(
            select(WorkflowRun)
            .where(
                WorkflowRun.status.in_([
                    "waiting_device", "waiting_confirmation", "waiting_ai", "retry_wait",
                    "paused_offline", "pending", "running",
                ]),
                WorkflowRun.deadline_at <= now,
            )
            .limit(limit)
        ).all()
        for row in rows:
            fail_run(session, row, error_payload("RUN_TIMEOUT", "workflow deadline elapsed", "run"), status="timed_out")
        if rows:
            session.commit()
        return len(rows)


async def run_workflow_scheduler(stop_event: asyncio.Event) -> None:
    interval = max(0.2, float(settings.workflow_scheduler_interval_seconds))
    logger.info("workflow scheduler started interval=%ss", interval)
    last_emit = time.time()
    last_cleanup = 0.0
    while not stop_event.is_set():
        tick_started = time.time()
        tick_error = ""
        try:
            reconcile_finished_dispatches()
            _expire_waiting_runs()
            with Session(engine) as session:
                expire_ai_interactions(session)
                expire_confirmations(session)
            _advance_ready_runs()
            process_pending_ai_interactions()
            await dispatch_pending_steps()
            await _emit_confirmation_updates(last_emit)
            await _emit_updated_runs(last_emit)
            last_emit = tick_started
            if tick_started - last_cleanup >= 3600:
                from api.services.workflows.result_store import cleanup_expired_results

                cleanup_expired_results(tick_started)
                last_cleanup = tick_started
        except Exception as exc:
            tick_error = str(exc)
            logger.exception("workflow scheduler tick failed")
        try:
            _record_heartbeat(tick_started, tick_error)
        except Exception:
            logger.exception("workflow scheduler heartbeat write failed")
        try:
            await asyncio.wait_for(stop_event.wait(), timeout=interval)
        except asyncio.TimeoutError:
            pass
    logger.info("workflow scheduler stopped")
