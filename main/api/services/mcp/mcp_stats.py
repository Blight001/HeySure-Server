"""Record + read MCP call outcomes and per-failure conversation locations.

``record_call`` is called from the AI worker for every tool call (best-effort —
it must never break a run). Reads power the failure-rate view in the web console
and the ``device+mcp.manage`` stats/failures actions, so an AI can see which of
its tools are flaky and jump to where each failure happened.
"""

import logging
import time
from typing import Any, Dict, List, Optional

from sqlmodel import Session, select

from api.database import engine
from api.models import McpFailureEvent, McpToolStat

logger = logging.getLogger(__name__)

# Keep at most this many failure events per (user, tool).
MAX_FAILURES_PER_TOOL = 50
_MAX_ERROR_LEN = 2000


def _int(value) -> Optional[int]:
    try:
        if value in (None, "", 0, "0"):
            return None
        return int(value)
    except (TypeError, ValueError):
        return None


def record_call(
    user_id: int,
    ai_config_id: Optional[int],
    tool: str,
    success: bool,
    error: str = "",
    session_id: str = "",
    run_id: str = "",
    message_id: Optional[int] = None,
) -> None:
    """Update the per-tool counter and, on failure, append a located event."""
    name = str(tool or "").strip()
    uid = _int(user_id)
    if not name or uid is None:
        return
    aid = _int(ai_config_id)
    now = time.time()
    try:
        with Session(engine) as session:
            stat = session.exec(
                select(McpToolStat).where(
                    McpToolStat.user_id == uid,
                    McpToolStat.ai_config_id == aid,
                    McpToolStat.tool == name,
                )
            ).first()
            if not stat:
                stat = McpToolStat(user_id=uid, ai_config_id=aid, tool=name)
                session.add(stat)
            stat.total = (stat.total or 0) + 1
            stat.last_called_at = now
            if not success:
                stat.failures = (stat.failures or 0) + 1
                stat.last_failure_at = now
                stat.last_error = str(error or "")[:_MAX_ERROR_LEN]
                session.add(McpFailureEvent(
                    user_id=uid,
                    ai_config_id=aid,
                    tool=name,
                    error=str(error or "")[:_MAX_ERROR_LEN],
                    session_id=str(session_id or ""),
                    run_id=str(run_id or ""),
                    message_id=_int(message_id),
                    created_at=now,
                ))
                events = session.exec(
                    select(McpFailureEvent)
                    .where(McpFailureEvent.user_id == uid, McpFailureEvent.tool == name)
                    .order_by(McpFailureEvent.created_at.desc(), McpFailureEvent.id.desc())
                ).all()
                for stale in events[MAX_FAILURES_PER_TOOL:]:
                    session.delete(stale)
            session.commit()
    except Exception:
        logger.exception("record_call failed for tool=%s user=%s", name, user_id)


def _stat_dict(rows: List[McpToolStat]) -> Dict[str, Dict[str, Any]]:
    """Aggregate one or more stat rows for the same tool across AI configs."""
    out: Dict[str, Dict[str, Any]] = {}
    for row in rows:
        agg = out.setdefault(row.tool, {
            "tool": row.tool, "total": 0, "failures": 0,
            "last_called_at": 0.0, "last_failure_at": 0.0, "last_error": "",
        })
        agg["total"] += int(row.total or 0)
        agg["failures"] += int(row.failures or 0)
        if float(row.last_called_at or 0) > agg["last_called_at"]:
            agg["last_called_at"] = float(row.last_called_at or 0)
        if float(row.last_failure_at or 0) > agg["last_failure_at"]:
            agg["last_failure_at"] = float(row.last_failure_at or 0)
            agg["last_error"] = str(row.last_error or "")
    for agg in out.values():
        total = agg["total"] or 0
        agg["failure_rate"] = round(agg["failures"] / total, 4) if total else 0.0
    return out


def tool_stats(user_id: int, tools: Optional[List[str]] = None) -> List[Dict[str, Any]]:
    """Failure-rate stats for a user, optionally restricted to ``tools``,
    aggregated across the user's AI configs."""
    uid = _int(user_id)
    if uid is None:
        return []
    want = {str(t).strip() for t in tools if str(t).strip()} if tools else None
    with Session(engine) as session:
        rows = session.exec(select(McpToolStat).where(McpToolStat.user_id == uid)).all()
    if want is not None:
        rows = [r for r in rows if r.tool in want]
    return sorted(_stat_dict(rows).values(), key=lambda s: (-s["failures"], s["tool"]))


def recent_failures(user_id: int, tool: str, limit: int = 20) -> List[Dict[str, Any]]:
    """Recent failures for one tool, each with its conversation location."""
    uid = _int(user_id)
    name = str(tool or "").strip()
    if uid is None or not name:
        return []
    with Session(engine) as session:
        rows = session.exec(
            select(McpFailureEvent)
            .where(McpFailureEvent.user_id == uid, McpFailureEvent.tool == name)
            .order_by(McpFailureEvent.created_at.desc(), McpFailureEvent.id.desc())
            .limit(max(1, min(int(limit or 1), MAX_FAILURES_PER_TOOL)))
        ).all()
    return [
        {
            "tool": r.tool,
            "error": r.error,
            "ai_config_id": r.ai_config_id,
            "session_id": r.session_id,
            "run_id": r.run_id,
            "message_id": r.message_id,
            "created_at": float(r.created_at or 0),
        }
        for r in rows
    ]
