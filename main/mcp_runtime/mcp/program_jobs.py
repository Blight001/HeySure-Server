"""Durable queue and worker for asynchronous ``mcp.program`` runs."""

from __future__ import annotations

import asyncio
import logging
import time
import uuid
from typing import Any, Dict, Optional

from fastapi import HTTPException
from sqlmodel import Session, select

from api.core.settings import settings
from api.database import engine
from api.models import McpProgramRun
from api.runtime.run_context import get_run_session_context, reset_run_session_context, set_run_session_context
from api.services.workflows.secrets import decrypt_json, encrypt_json

from tools.program import ProgramCancelled, execute_program, validate_program


logger = logging.getLogger(__name__)
TERMINAL_STATUSES = {"completed", "failed", "cancelled", "expired"}


def _now() -> float:
    return time.time()


def _decode(value: Optional[str], fallback: Any) -> Any:
    try:
        return decrypt_json(value or "")
    except Exception:
        return fallback


def _owned(session: Session, run_id: str, user_id: int, ai_config_id: Optional[int]) -> McpProgramRun:
    row = session.get(McpProgramRun, str(run_id or ""))
    if not row or row.user_id != user_id or (
        ai_config_id is not None and row.ai_config_id != ai_config_id
    ):
        raise HTTPException(status_code=404, detail="MCP program run not found")
    return row


def _payload(row: McpProgramRun) -> Dict[str, Any]:
    payload: Dict[str, Any] = {
        "run_id": row.id,
        "status": row.status,
        "user_id": row.user_id,
        "ai_config_id": row.ai_config_id,
        "created_at": row.created_at,
        "updated_at": row.updated_at,
        "started_at": row.started_at,
        "finished_at": row.finished_at,
        "deadline_at": row.deadline_at,
    }
    if row.output_json:
        payload["output"] = _decode(row.output_json, {"code": "OUTPUT_DECODE_FAILED"})
    if row.error_json:
        payload["error"] = _decode(row.error_json, {"code": "ERROR_DECODE_FAILED"})
    return payload


def enqueue_program(
    *,
    user_id: int,
    ai_config_id: Optional[int],
    program: Dict[str, Any],
    input_value: Dict[str, Any],
    allowed_tools: Optional[set[str]],
) -> Dict[str, Any]:
    if not settings.mcp_program_async_enabled:
        raise HTTPException(status_code=503, detail="asynchronous MCP programs are disabled")
    normalized = validate_program(program)
    now = _now()
    run_id = f"mcpprogram_{uuid.uuid4().hex}"
    context = get_run_session_context() or {}
    row = McpProgramRun(
        id=run_id,
        user_id=user_id,
        ai_config_id=ai_config_id,
        status="queued",
        program_json=encrypt_json(normalized),
        input_json=encrypt_json(input_value),
        allowed_tools_json=encrypt_json(sorted(allowed_tools) if allowed_tools is not None else None),
        session_context_json=encrypt_json(context),
        deadline_at=now + int(settings.mcp_program_max_runtime_seconds),
        created_at=now,
        updated_at=now,
    )
    with Session(engine) as session:
        session.add(row)
        session.commit()
        session.refresh(row)
        return _payload(row)


def get_program(*, user_id: int, ai_config_id: Optional[int], run_id: str) -> Dict[str, Any]:
    with Session(engine) as session:
        return _payload(_owned(session, run_id, user_id, ai_config_id))


def cancel_program(*, user_id: int, ai_config_id: Optional[int], run_id: str) -> Dict[str, Any]:
    now = _now()
    with Session(engine) as session:
        row = _owned(session, run_id, user_id, ai_config_id)
        if row.status not in TERMINAL_STATUSES:
            row.cancel_requested = True
            row.updated_at = now
            if row.status == "queued":
                row.status = "cancelled"
                row.finished_at = now
            session.add(row)
            session.commit()
            session.refresh(row)
        payload = _payload(row)
    if payload["status"] == "running":
        payload["cancellation_requested"] = True
    return payload


def _claim_next(worker_id: str) -> Optional[str]:
    now = _now()
    with Session(engine) as session:
        expired = session.exec(
            select(McpProgramRun).where(
                McpProgramRun.status == "queued",
                McpProgramRun.deadline_at <= now,
            )
        ).all()
        for row in expired:
            row.status = "expired"
            row.error_json = encrypt_json({"code": "DEADLINE_EXCEEDED"})
            row.finished_at = now
            row.updated_at = now
            session.add(row)
        # Do not silently replay a side-effecting program after a worker dies.
        # A lease expiry is an explicit terminal failure; the caller can submit
        # a new idempotent run after inspecting the result.
        stale = session.exec(
            select(McpProgramRun).where(
                McpProgramRun.status == "running",
                McpProgramRun.lease_until.is_not(None),
                McpProgramRun.lease_until < now,
            )
        ).all()
        for row in stale:
            row.status = "failed"
            row.error_json = encrypt_json({"code": "WORKER_LEASE_EXPIRED"})
            row.finished_at = now
            row.updated_at = now
            row.owner = ""
            session.add(row)
        session.commit()

        statement = (
            select(McpProgramRun)
            .where(
                McpProgramRun.status == "queued",
                McpProgramRun.cancel_requested == False,  # noqa: E712
                McpProgramRun.deadline_at > now,
            )
            .order_by(McpProgramRun.created_at.asc())
            .limit(1)
        )
        try:
            row = session.exec(statement.with_for_update(skip_locked=True)).first()
        except TypeError:
            row = session.exec(statement).first()
        if not row:
            return None
        row.status = "running"
        row.owner = worker_id
        row.lease_until = min(
            row.deadline_at,
            now + int(settings.mcp_program_lease_seconds),
        )
        row.started_at = row.started_at or now
        row.updated_at = now
        session.add(row)
        session.commit()
        return row.id


def _cancel_requested(run_id: str) -> bool:
    with Session(engine) as session:
        row = session.get(McpProgramRun, run_id)
        return not row or bool(row.cancel_requested) or row.status in {"cancelled", "expired"}


def _finish(run_id: str, *, status: str, output: Any = None, error: Any = None) -> None:
    now = _now()
    with Session(engine) as session:
        row = session.get(McpProgramRun, run_id)
        if not row:
            return
        if row.cancel_requested and status == "completed":
            status = "cancelled"
            error = {"code": "CANCELLED"}
            output = None
        if now >= row.deadline_at and status not in {"cancelled", "failed"}:
            status = "expired"
            error = {"code": "DEADLINE_EXCEEDED"}
            output = None
        row.status = status
        row.output_json = encrypt_json(output) if output is not None else None
        row.error_json = encrypt_json(error) if error is not None else None
        row.owner = ""
        row.lease_until = None
        row.finished_at = now
        row.updated_at = now
        session.add(row)
        session.commit()


async def _run_claimed(run_id: str) -> None:
    with Session(engine) as session:
        row = session.get(McpProgramRun, run_id)
        if not row:
            return
        program = _decode(row.program_json, {})
        input_value = _decode(row.input_json, {})
        allowed = _decode(row.allowed_tools_json, [])
        context = _decode(row.session_context_json, {})
        user_id = row.user_id
        ai_config_id = row.ai_config_id
    token = set_run_session_context(context if isinstance(context, dict) else None)
    try:
        result = await execute_program(
            program,
            user_id=user_id,
            ai_config_id=ai_config_id,
            input_value=input_value if isinstance(input_value, dict) else {},
            allowed_tools=(
                {str(item) for item in allowed}
                if isinstance(allowed, list)
                else None
            ),
            cancel_check=lambda: _cancel_requested(run_id),
            program_id=run_id,
        )
    except ProgramCancelled:
        _finish(run_id, status="cancelled", error={"code": "CANCELLED"})
    except Exception as exc:
        logger.warning("MCP program failed run_id=%s error=%s", run_id, type(exc).__name__)
        _finish(run_id, status="failed", error={"code": type(exc).__name__, "message": str(exc)[:500]})
    else:
        _finish(run_id, status="completed", output=result)
    finally:
        reset_run_session_context(token)


async def program_worker_loop(stop_event: asyncio.Event) -> None:
    """Run one DB-backed worker loop owned by the MCP runtime process."""

    worker_id = f"mcp-program-{uuid.uuid4().hex[:12]}"
    while not stop_event.is_set():
        run_id = await asyncio.to_thread(_claim_next, worker_id)
        if run_id:
            await _run_claimed(run_id)
            continue
        try:
            await asyncio.wait_for(stop_event.wait(), timeout=settings.mcp_program_worker_poll_seconds)
        except asyncio.TimeoutError:
            pass
