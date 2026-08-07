"""Connector-only workflow compensation and advancement loop."""

from __future__ import annotations

import asyncio
import logging
import time

from sqlmodel import Session, select

from api.core.settings import settings
from api.database import engine
from api.models import WorkflowRun
from api.services.workflows.run_service import advance_run, error_payload, fail_run
from connector_runtime.dispatch.workflow_dispatch import (
    dispatch_pending_steps,
    reconcile_finished_dispatches,
)


logger = logging.getLogger(__name__)


def _advance_ready_runs(limit: int = 100) -> int:
    now = time.time()
    with Session(engine) as session:
        rows = session.exec(
            select(WorkflowRun)
            .where(
                WorkflowRun.status.in_(["pending", "running"]),
                WorkflowRun.next_wakeup_at <= now,
            )
            .order_by(WorkflowRun.next_wakeup_at, WorkflowRun.created_at)
            .limit(limit)
        ).all()
    count = 0
    for row in rows:
        with Session(engine) as session:
            advance_run(session, row.id)
            count += 1
    return count


def _expire_waiting_runs(limit: int = 100) -> int:
    now = time.time()
    with Session(engine) as session:
        rows = session.exec(
            select(WorkflowRun)
            .where(
                WorkflowRun.status.in_(["waiting_device", "paused_offline", "pending", "running"]),
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
    while not stop_event.is_set():
        try:
            reconcile_finished_dispatches()
            _expire_waiting_runs()
            _advance_ready_runs()
            await dispatch_pending_steps()
        except Exception:
            logger.exception("workflow scheduler tick failed")
        try:
            await asyncio.wait_for(stop_event.wait(), timeout=interval)
        except asyncio.TimeoutError:
            pass
    logger.info("workflow scheduler stopped")
