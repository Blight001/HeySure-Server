"""Connector-specific queue and live-socket health metrics."""

from __future__ import annotations

import time
from typing import Any, Dict

from sqlalchemy import func
from sqlmodel import Session, select

from api.database import engine
from api.models import AgentDispatchTask
from api.sio import agents


def connector_health_detail() -> Dict[str, Any]:
    now = time.time()
    with Session(engine) as session:
        pending = session.exec(
            select(func.count()).select_from(AgentDispatchTask).where(AgentDispatchTask.status == "pending")
        ).one()
        queued = session.exec(
            select(func.count()).select_from(AgentDispatchTask).where(AgentDispatchTask.status == "queued")
        ).one()
        oldest = session.exec(
            select(func.min(AgentDispatchTask.created_at)).where(AgentDispatchTask.status == "pending")
        ).one()
        last_result = session.exec(
            select(func.max(AgentDispatchTask.completed_at)).where(
                AgentDispatchTask.status.in_(["completed", "error", "timeout", "cancelled"])
            )
        ).one()
    dispatchable = sum(bool(agent.get("toolDefs") or agent.get("tools")) for agent in agents.values())
    return {
        "connected_agent_count": len(agents),
        "dispatchable_agent_count": dispatchable,
        "pending_dispatch_count": int(pending or 0),
        "queued_dispatch_count": int(queued or 0),
        "oldest_pending_age_seconds": round(max(0.0, now - oldest), 3) if oldest else None,
        "last_result_at": last_result,
    }
