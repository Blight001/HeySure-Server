"""Safe, user-scoped payloads for workflow confirmation notifications."""

from __future__ import annotations

import time
from typing import Any, Dict, List, Optional, Tuple

from sqlmodel import Session, select

from api.models import AssistantAIConfig, WorkflowCard, WorkflowConfirmation, WorkflowRun


USER_CONFIRMATION_TYPES = frozenset({"explicit", "forced", "user_via_ai", "user_via_ai_dispatch"})


def notification_room(user_id: int) -> str:
    return f"workflow_confirmations_{int(user_id)}"


def _clean_text(value: object, limit: int) -> str:
    return " ".join(str(value or "").split())[:limit]


def notification_payload(session: Session, item: WorkflowConfirmation) -> Dict[str, Any]:
    """Build a lock-screen-safe payload without inputs, tool arguments, or credentials."""
    run = session.get(WorkflowRun, item.run_id)
    card = session.get(WorkflowCard, run.card_id) if run else None
    actor = None
    if item.ai_config_id:
        actor = session.get(AssistantAIConfig, item.ai_config_id)
    return {
        "confirmation_id": item.id,
        "run_id": item.run_id,
        "requested_user_id": item.requested_user_id,
        "card_id": run.card_id if run else "",
        "card_name": _clean_text(card.name if card else "自动化卡片", 80),
        "actor_name": _clean_text(actor.name if actor else "", 80),
        "risk_summary": _clean_text(item.risk_summary, 300),
        "type": item.confirmation_type,
        "status": item.status,
        "created_at": item.created_at,
        "expires_at": item.expires_at,
    }


def pending_notifications(
    session: Session,
    *,
    user_id: int,
    now: Optional[float] = None,
    limit: int = 100,
) -> List[Dict[str, Any]]:
    current = float(now or time.time())
    rows = session.exec(
        select(WorkflowConfirmation)
        .where(
            WorkflowConfirmation.requested_user_id == int(user_id),
            WorkflowConfirmation.status == "pending",
            WorkflowConfirmation.confirmation_type.in_(USER_CONFIRMATION_TYPES),
            WorkflowConfirmation.expires_at > current,
        )
        .order_by(WorkflowConfirmation.created_at)
        .limit(limit)
    ).all()
    return [notification_payload(session, item) for item in rows]


def notification_events_since(
    session: Session,
    *,
    since: float,
    now: Optional[float] = None,
    limit: int = 200,
) -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]]]:
    """Return new pending requests and terminal decisions for Socket.IO delivery."""
    current = float(now or time.time())
    requested = session.exec(
        select(WorkflowConfirmation)
        .where(
            WorkflowConfirmation.created_at >= float(since),
            WorkflowConfirmation.status == "pending",
            WorkflowConfirmation.confirmation_type.in_(USER_CONFIRMATION_TYPES),
            WorkflowConfirmation.expires_at > current,
        )
        .order_by(WorkflowConfirmation.created_at)
        .limit(limit)
    ).all()
    resolved = session.exec(
        select(WorkflowConfirmation)
        .where(
            WorkflowConfirmation.decided_at.is_not(None),
            WorkflowConfirmation.decided_at >= float(since),
            WorkflowConfirmation.status != "pending",
            WorkflowConfirmation.confirmation_type.in_(USER_CONFIRMATION_TYPES),
        )
        .order_by(WorkflowConfirmation.decided_at)
        .limit(limit)
    ).all()
    request_payloads = [notification_payload(session, item) for item in requested]
    resolution_payloads = [
        {
            "confirmation_id": item.id,
            "run_id": item.run_id,
            "status": item.status,
            "decided_at": item.decided_at,
            "requested_user_id": item.requested_user_id,
        }
        for item in resolved
    ]
    return request_payloads, resolution_payloads
