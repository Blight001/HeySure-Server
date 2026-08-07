"""Append-only workflow audit helpers with pre-redacted details."""

from __future__ import annotations

import json
import logging
import time
from typing import Any, Dict, Optional

from sqlmodel import Session

from api.models import WorkflowAuditEvent, WorkflowRun


logger = logging.getLogger(__name__)


def add_audit(
    session: Session,
    *,
    event_type: str,
    run: Optional[WorkflowRun] = None,
    user_id: Optional[int] = None,
    step_id: str = "",
    dispatch_task_id: str = "",
    status_from: str = "",
    status_to: str = "",
    detail: Optional[Dict[str, Any]] = None,
) -> WorkflowAuditEvent:
    if run is None and user_id is None:
        raise ValueError("audit event requires run or user_id")
    row = WorkflowAuditEvent(
        user_id=int(run.user_id if run else user_id),
        run_id=run.id if run else None,
        card_id=run.card_id if run else None,
        card_version_id=run.card_version_id if run else None,
        step_id=step_id,
        dispatch_task_id=dispatch_task_id,
        device_id=run.device_id if run else "",
        event_type=event_type,
        status_from=status_from,
        status_to=status_to,
        detail_json=json.dumps(detail or {}, ensure_ascii=False, separators=(",", ":"), default=str),
    )
    session.add(row)
    logger.info(
        "workflow transition",
        extra={
            "workflow_run_id": row.run_id or "",
            "card_version_id": row.card_version_id or "",
            "step_id": row.step_id,
            "dispatch_task_id": row.dispatch_task_id,
            "device_id": row.device_id,
            "status_from": row.status_from,
            "status_to": row.status_to,
            "duration_ms": int(max(0.0, time.time() - run.started_at) * 1000) if run and run.started_at else 0,
            "workflow_event_type": event_type,
            "workflow_error_code": str((detail or {}).get("error", {}).get("code") or "")
            if isinstance((detail or {}).get("error"), dict) else "",
        },
    )
    return row
