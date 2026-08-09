"""Timeout and cancellation terminalization for endpoint dispatches."""

import logging
import time

from sqlmodel import Session, select

from api.models import AgentDispatchTask
from api.sio import sio
from connector_runtime.dispatch import repository


logger = logging.getLogger(__name__)


async def expire_dispatch(task_id: str, reason: str = "result wait timed out") -> bool:
    dispatch = _dispatch_module()
    ctx = dispatch._PENDING_DISPATCHES.pop(task_id, None)
    device_id = str(ctx.get("device_id") or "") if ctx else ""
    if not device_id:
        row = _dispatch_row(task_id)
        if not row or row.status not in {"pending", "queued"}:
            return False
        device_id = str(row.device_id or "")
    expired = repository.finalize_dispatch_row(
        task_id, status="timeout", success=False, error=reason
    )
    if expired and device_id:
        try:
            await dispatch.resume_device_dispatch_queue(device_id)
        except Exception:
            logger.exception("queue resume after expire failed device=%s", device_id)
    return expired


async def cancel_dispatch(task_id: str, reason: str = "cancelled by user") -> bool:
    dispatch = _dispatch_module()
    ctx = dispatch._PENDING_DISPATCHES.pop(task_id, None)
    ctx = ctx or dispatch._resolve_result_context({"taskId": task_id})
    device_id = str(ctx.get("device_id") or "")
    cancelled = repository.finalize_dispatch_row(
        task_id, status="cancelled", success=False, error=reason
    )
    recorded = cancelled or _row_has_status(task_id, "cancelled")
    if not recorded:
        return False
    _release_waiter(dispatch, task_id, device_id, ctx, reason)
    target_sid = dispatch._find_agent_sid(device_id)
    if target_sid:
        await sio.emit(
            "task:cancel",
            {"taskId": task_id, "reason": reason},
            to=target_sid,
        )
    await dispatch._emit_to_user(ctx, "device:task_cancelled", {
        "taskId": task_id,
        "deviceId": device_id,
        "sessionId": ctx.get("session_id"),
        "reason": reason,
        "updatedAt": time.time(),
    })
    if device_id:
        await dispatch.resume_device_dispatch_queue(device_id)
    return True


def _release_waiter(dispatch, task_id, device_id, ctx, reason) -> None:
    waiter = dispatch._PENDING_DISPATCH_WAITERS.pop(task_id, None)
    if not waiter:
        return
    loop = waiter.get("loop")
    future = waiter.get("future")
    if loop and future and not future.done():
        loop.call_soon_threadsafe(future.set_result, {
            "success": False,
            "taskId": task_id,
            "deviceId": device_id,
            "tool": str(ctx.get("tool") or ""),
            "error": reason,
            "cancelled": True,
            "result": None,
        })


def _dispatch_row(task_id):
    try:
        with Session(repository.engine) as session:
            return session.exec(
                select(AgentDispatchTask).where(AgentDispatchTask.task_id == task_id)
            ).first()
    except Exception:
        logger.exception("dispatch lookup failed task=%s", task_id)
        return None


def _row_has_status(task_id: str, status: str) -> bool:
    row = _dispatch_row(task_id)
    return bool(row and row.status == status)


def _dispatch_module():
    from connector_runtime.dispatch import device_dispatch

    return device_dispatch
