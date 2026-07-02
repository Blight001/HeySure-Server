"""
OPT-03: Desktop Agent task dispatch + result handling.

Bridges the server task system and connected desktop agents over Socket.IO.

Socket protocol:
    device:register   agent → server   { id, name, platform, capabilities[], version }
    task:dispatch    server → agent   { taskId, userId, aiConfigId, sessionId,
                                         instruction, tool, args, allowedTools[] }
    task:progress    agent → server   { taskId, deviceId, message }
    task:result      agent → server   { taskId, deviceId, success, tool, result, summary }
    task:error       agent → server   { taskId, deviceId, error }

Results are persisted into the originating chat session and broadcast to the
user's UI room so the frontend updates live.
"""

import contextvars
import asyncio
import json
import logging
import threading
import time
import uuid
from typing import Any, Dict, List, Optional

from sqlmodel import Session, select

from api.database import engine
from connector_runtime.dispatch.desktop_device_tools import (
    get_connected_browser_agent,
    get_connected_desktop_agent,
    is_browser_tool,
    is_desktop_tool,
    is_endpoint_agent_tool,
    is_workshop_tool,
)
from api.services.storage.screenshot_store import attach_persisted_screenshot
from api.models import AgentDispatchTask, ChatMessageCreate
from api.sio import agents, sio
from api.services.chat.chat_persistence import _save_message


logger = logging.getLogger(__name__)
_DISPATCH_QUEUE_LOCK = threading.Lock()


def _persist_dispatch(
    *,
    task_id: str,
    user_id: int,
    ai_config_id: Optional[int],
    ai_kind: str,
    session_id: str,
    session_name: Optional[str],
    device_id: str,
    tool: str,
    instruction: str,
    args: Optional[Dict[str, Any]] = None,
    suppress_session_message: bool = False,
    status: str = "pending",
) -> None:
    """Insert a ``pending`` row so connector-runtime restarts can still
    deliver the eventual result to whoever is polling by ``task_id``."""
    try:
        with Session(engine) as session:
            session.add(AgentDispatchTask(
                task_id=task_id,
                user_id=user_id,
                ai_config_id=ai_config_id,
                ai_kind=ai_kind or "assistant",
                session_id=session_id,
                session_name=session_name,
                device_id=device_id,
                tool=tool or "",
                instruction=instruction or "",
                args_json=json.dumps(args or {}, ensure_ascii=False, default=str),
                suppress_session_message=bool(suppress_session_message),
                status=status,
            ))
            session.commit()
    except Exception as exc:
        # Persistence failure is non-fatal for the in-memory dispatch path,
        # but it does defeat the restart-resilience guarantee. Log loudly.
        logger.exception(f"persist failed task={task_id}: {exc}")


def _enqueue_dispatch_row(
    *,
    task_id: str,
    user_id: int,
    ai_config_id: Optional[int],
    ai_kind: str,
    session_id: str,
    session_name: Optional[str],
    device_id: str,
    tool: str,
    instruction: str,
    args: Optional[Dict[str, Any]],
    suppress_session_message: bool,
) -> str:
    """Atomically choose ``pending`` or ``queued`` for one device.

    The gateway owns endpoint sockets, so this process lock closes the race
    between concurrent chat workers checking and inserting dispatch rows.
    """
    with _DISPATCH_QUEUE_LOCK:
        with Session(engine) as session:
            ahead = session.exec(
                select(AgentDispatchTask).where(
                    AgentDispatchTask.device_id == device_id,
                    AgentDispatchTask.status.in_(["pending", "queued"]),
                )
            ).first()
            status = "queued" if ahead else "pending"
            session.add(AgentDispatchTask(
                task_id=task_id,
                user_id=user_id,
                ai_config_id=ai_config_id,
                ai_kind=ai_kind or "assistant",
                session_id=session_id,
                session_name=session_name,
                device_id=device_id,
                tool=tool or "",
                instruction=instruction or "",
                args_json=json.dumps(args or {}, ensure_ascii=False, default=str),
                suppress_session_message=bool(suppress_session_message),
                status=status,
            ))
            session.commit()
            return status


def _finalize_dispatch_row(
    task_id: str,
    *,
    status: str,
    success: Optional[bool] = None,
    summary: Optional[str] = None,
    result: Any = None,
    error: Optional[str] = None,
) -> None:
    """Mark a dispatch row finished.

    Idempotent for rows that already terminated as ``completed`` or ``error``
    (real result of an agent reply). Allows overwriting a ``timeout`` row —
    if the agent eventually replies after the orphan sweep marked it timed
    out, we preserve the actual result for audit even though the chat_worker
    has already moved on.
    """
    try:
        with Session(engine) as session:
            row = session.exec(
                select(AgentDispatchTask).where(AgentDispatchTask.task_id == task_id)
            ).first()
            if not row:
                return
            if row.status in {"completed", "error"}:
                return  # Real outcome already recorded; don't overwrite.
            row.status = status
            row.success = success
            row.summary = summary
            if result is not None:
                try:
                    row.result_json = json.dumps(result, ensure_ascii=False, default=str)
                except Exception:
                    row.result_json = str(result)
            row.error = error
            row.completed_at = time.time()
            session.add(row)
            session.commit()
    except Exception as exc:
        logger.exception(f"finalize failed task={task_id}: {exc}")


def expire_orphan_dispatches(older_than_seconds: float = 300.0) -> int:
    """Mark pending/queued rows that have been waiting too long as ``timeout``.

    Called on connector-runtime startup (and periodically) to clean up rows
    whose original Future died with a previous process — pollers were stuck
    looking at them. Queued rows are expired too: replaying a minutes-old
    click/navigate once the queue unblocks does more harm than dropping it.
    """
    cutoff = time.time() - older_than_seconds
    expired = 0
    try:
        with Session(engine) as session:
            rows = session.exec(
                select(AgentDispatchTask).where(
                    AgentDispatchTask.status.in_(["pending", "queued"])
                )
            ).all()
            for row in rows:
                if (row.created_at or 0) < cutoff:
                    stale_kind = row.status
                    row.status = "timeout"
                    row.error = row.error or (
                        "orphaned across connector-runtime restart"
                        if stale_kind == "pending"
                        else "expired in device queue before dispatch"
                    )
                    row.completed_at = time.time()
                    session.add(row)
                    expired += 1
            if expired:
                session.commit()
    except Exception as exc:
        logger.exception(f"orphan sweep failed: {exc}")
    return expired


async def expire_dispatch(task_id: str, reason: str = "result wait timed out") -> bool:
    """Finalize one unfinished dispatch as ``timeout`` and unblock its device queue.

    Called (via the gateway's internal expire endpoint) by pollers in other
    processes when they give up waiting on ``task_id`` — the row would
    otherwise keep the device's queue wedged until the orphan sweep. Returns
    False when the task already reached a terminal state.
    """
    ctx = _PENDING_DISPATCHES.pop(task_id, None)
    device_id = str(ctx.get("device_id") or "") if ctx else ""
    if not device_id:
        try:
            with Session(engine) as session:
                row = session.exec(
                    select(AgentDispatchTask).where(AgentDispatchTask.task_id == task_id)
                ).first()
            if not row or row.status not in {"pending", "queued"}:
                return False
            device_id = str(row.device_id or "")
        except Exception as exc:
            logger.exception(f"expire lookup failed task={task_id}: {exc}")
            return False
    _finalize_dispatch_row(task_id, status="timeout", success=False, error=reason)
    if device_id:
        try:
            await resume_device_dispatch_queue(device_id)
        except Exception:
            logger.exception(f"queue resume after expire failed device={device_id}")
    return True

# Per-run session context so MCP tools (running inside the worker thread) can
# attach dispatched-task results to the correct chat session. asyncio.run()
# copies the current context, so a value set before the tool call is visible
# inside the (async) MCP handler.
_RUN_SESSION_CONTEXT: contextvars.ContextVar[Optional[Dict[str, Any]]] = contextvars.ContextVar(
    "run_session_context",
    default=None,
)

# taskId -> dispatch context (for routing results back to a session).
_PENDING_DISPATCHES: Dict[str, Dict[str, Any]] = {}
_PENDING_DISPATCH_WAITERS: Dict[str, Dict[str, Any]] = {}

# Dispatches with no agent reply after this many seconds are considered lost and
# are dropped so the in-memory map does not grow unbounded when an agent drops.
_DISPATCH_TTL_SECONDS = 1800


def purge_stale_dispatches(now: Optional[float] = None) -> int:
    now = now if now is not None else time.time()
    stale = [
        task_id
        for task_id, ctx in _PENDING_DISPATCHES.items()
        if now - float(ctx.get("created_at") or 0) > _DISPATCH_TTL_SECONDS
    ]
    for task_id in stale:
        _PENDING_DISPATCHES.pop(task_id, None)
    return len(stale)


def _update_agent_task_state(device_id: str, *, status: str, task_id: str, error: Optional[str] = None) -> None:
    for agent in agents.values():
        if str(agent.get("id")) == str(device_id):
            agent["lastTaskId"] = task_id
            agent["lastTaskStatus"] = status
            agent["lastTaskAt"] = time.time()
            agent["lastSeenAt"] = time.time()
            agent["lastError"] = error
            break


def set_run_session_context(ctx: Optional[Dict[str, Any]]):
    return _RUN_SESSION_CONTEXT.set(ctx or None)


def get_run_session_context() -> Optional[Dict[str, Any]]:
    return _RUN_SESSION_CONTEXT.get()


def _find_agent_sid(device_id: str) -> Optional[str]:
    for sid, agent in agents.items():
        if str(agent.get("id")) == str(device_id):
            return sid
    return None


def _device_kind_label(device_id: str) -> str:
    for agent in agents.values():
        if str(agent.get("id")) != str(device_id):
            continue
        platform = str(agent.get("platform") or "").lower()
        if bool(agent.get("isWorkshop")) or "workshop" in platform:
            return "图书馆Agent"
        if bool(agent.get("isBrowserExtension")) or "browser-extension" in platform:
            return "浏览器Agent"
        if bool(agent.get("isAndroid")) or "android" in platform:
            return "安卓端Agent"
        if bool(agent.get("isWindowsDesktop")) or "desktop" in platform:
            return "桌面端Agent"
        return "端侧Agent"
    return "端侧Agent"


def _context_from_dispatch_row(row: AgentDispatchTask) -> Dict[str, Any]:
    try:
        args = json.loads(row.args_json or "{}")
    except Exception:
        args = {}
    return {
        "device_id": row.device_id,
        "user_id": row.user_id,
        "ai_config_id": row.ai_config_id,
        "ai_kind": row.ai_kind or "assistant",
        "session_id": row.session_id or "",
        "session_name": row.session_name,
        "model": None,
        "instruction": row.instruction or "",
        "tool": row.tool or "",
        "args": args if isinstance(args, dict) else {},
        "created_at": row.created_at,
        "suppress_session_message": bool(row.suppress_session_message),
    }


async def resume_device_dispatch_queue(device_id: str) -> Optional[str]:
    """Dispatch the oldest queued task when ``device_id`` has no active task."""
    target_sid = _find_agent_sid(device_id)
    if not target_sid:
        return None

    with _DISPATCH_QUEUE_LOCK:
        with Session(engine) as session:
            active = session.exec(
                select(AgentDispatchTask).where(
                    AgentDispatchTask.device_id == device_id,
                    AgentDispatchTask.status == "pending",
                )
            ).first()
            if active:
                return None
            row = session.exec(
                select(AgentDispatchTask).where(
                    AgentDispatchTask.device_id == device_id,
                    AgentDispatchTask.status == "queued",
                ).order_by(AgentDispatchTask.created_at, AgentDispatchTask.id)
            ).first()
            if not row:
                return None
            row.status = "pending"
            session.add(row)
            session.commit()
            session.refresh(row)
            ctx = _context_from_dispatch_row(row)

    task_id = str(row.task_id)
    _PENDING_DISPATCHES[task_id] = ctx
    payload = {
        "taskId": task_id,
        "userId": ctx["user_id"],
        "aiConfigId": ctx["ai_config_id"],
        "sessionId": ctx["session_id"],
        "instruction": ctx["instruction"],
        "tool": ctx["tool"],
        "args": ctx["args"],
        "allowedTools": [ctx["tool"]] if ctx["tool"] else [],
    }
    try:
        await sio.emit("task:dispatch", payload, to=target_sid)
    except Exception:
        _PENDING_DISPATCHES.pop(task_id, None)
        with Session(engine) as session:
            failed = session.exec(
                select(AgentDispatchTask).where(AgentDispatchTask.task_id == task_id)
            ).first()
            if failed and failed.status == "pending":
                failed.status = "queued"
                session.add(failed)
                session.commit()
        raise
    return task_id


async def dispatch_task_to_agent(
    *,
    device_id: str,
    user_id: int,
    ai_config_id: Optional[int],
    ai_kind: str,
    session_id: str,
    session_name: Optional[str],
    model: Optional[str],
    instruction: str,
    tool: str = "",
    args: Optional[Dict[str, Any]] = None,
    allowed_tools: Optional[List[str]] = None,
    wait_for_result: bool = False,
    timeout_seconds: int = 120,
    suppress_session_message: bool = False,
) -> Dict[str, Any]:
    target_sid = _find_agent_sid(device_id)
    if not target_sid:
        return {"success": False, "error": f"Agent not connected: {device_id}"}

    purge_stale_dispatches()
    task_id = f"atask_{uuid.uuid4().hex[:12]}"
    payload = {
        "taskId": task_id,
        "userId": user_id,
        "aiConfigId": ai_config_id,
        "sessionId": session_id,
        "instruction": instruction,
        "tool": tool or "",
        "args": args or {},
        "allowedTools": allowed_tools or [],
    }
    dispatch_ctx = {
        "device_id": device_id,
        "user_id": user_id,
        "ai_config_id": ai_config_id,
        "ai_kind": ai_kind or "assistant",
        "session_id": session_id,
        "session_name": session_name,
        "model": model,
        "instruction": instruction,
        "tool": tool or "",
        "args": args or {},
        "created_at": time.time(),
        "suppress_session_message": bool(suppress_session_message),
    }
    dispatch_status = _enqueue_dispatch_row(
        task_id=task_id,
        user_id=user_id,
        ai_config_id=ai_config_id,
        ai_kind=ai_kind,
        session_id=session_id,
        session_name=session_name,
        device_id=device_id,
        tool=tool or "",
        instruction=instruction or "",
        args=args or {},
        suppress_session_message=suppress_session_message,
    )
    _PENDING_DISPATCHES[task_id] = dispatch_ctx
    waiter = None
    if wait_for_result:
        loop = asyncio.get_running_loop()
        waiter = {"loop": loop, "future": loop.create_future()}
        _PENDING_DISPATCH_WAITERS[task_id] = waiter
    if dispatch_status == "pending":
        try:
            await sio.emit("task:dispatch", payload, to=target_sid)
        except Exception as exc:
            _PENDING_DISPATCHES.pop(task_id, None)
            _finalize_dispatch_row(task_id, status="error", success=False, error=str(exc))
            await resume_device_dispatch_queue(device_id)
            raise
    else:
        promoted_task_id = await resume_device_dispatch_queue(device_id)
        if promoted_task_id == task_id:
            dispatch_status = "pending"
    if wait_for_result and waiter:
        future = waiter["future"]
        try:
            return await asyncio.wait_for(future, timeout=max(1, int(timeout_seconds or 120)))
        except asyncio.TimeoutError:
            # Finalize the row and let the device queue continue right away.
            # Leaving it "pending" would block every later dispatch to this
            # device until the orphan sweep (~5 min). A late agent reply still
            # overwrites the timeout row for audit (see _finalize_dispatch_row).
            _PENDING_DISPATCHES.pop(task_id, None)
            _finalize_dispatch_row(
                task_id,
                status="timeout",
                success=False,
                error=f"Endpoint agent result timeout after {timeout_seconds}s",
            )
            try:
                await resume_device_dispatch_queue(device_id)
            except Exception:
                logger.exception(f"queue resume after waiter timeout failed device={device_id}")
            return {
                "success": False,
                "taskId": task_id,
                "deviceId": device_id,
                "tool": tool or "",
                "error": f"Endpoint agent result timeout after {timeout_seconds}s",
            }
        finally:
            _PENDING_DISPATCH_WAITERS.pop(task_id, None)
    return {
        "success": True,
        "taskId": task_id,
        "deviceId": device_id,
        "status": dispatch_status,
        "note": (
            f"Task dispatched to {_device_kind_label(device_id)}."
            if dispatch_status == "pending"
            else f"Task queued for {_device_kind_label(device_id)}; it will run after the current task."
        ),
    }


def _resolve_result_context(data: Dict[str, Any]) -> Dict[str, Any]:
    """Prefer the tracked dispatch context; fall back to the persisted row,
    then to fields echoed by the agent.

    The DB fallback matters for late replies (a reply arriving after the
    waiter timed out and dropped the in-memory context): it preserves
    ``suppress_session_message`` and the session identity, so a re-delivered
    result doesn't spam the chat session.
    """
    task_id = str(data.get("taskId") or "")
    ctx = _PENDING_DISPATCHES.get(task_id)
    if ctx:
        return ctx
    if task_id:
        try:
            with Session(engine) as session:
                row = session.exec(
                    select(AgentDispatchTask).where(AgentDispatchTask.task_id == task_id)
                ).first()
            if row:
                return _context_from_dispatch_row(row)
        except Exception as exc:
            logger.exception(f"result context lookup failed task={task_id}: {exc}")
    return {
        "device_id": str(data.get("deviceId") or "unknown"),
        "user_id": data.get("userId"),
        "ai_config_id": data.get("aiConfigId"),
        "ai_kind": data.get("aiKind") or "assistant",
        "session_id": data.get("sessionId"),
        "session_name": None,
        "model": None,
        "instruction": data.get("instruction") or "",
        "tool": data.get("tool") or "",
        "args": data.get("args") if isinstance(data.get("args"), dict) else {},
    }


def _save_agent_message(ctx: Dict[str, Any], content: str, tags: str) -> None:
    user_id = ctx.get("user_id")
    session_id = ctx.get("session_id")
    if not user_id or not session_id:
        return
    with Session(engine) as session:
        _save_message(
            session,
            int(user_id),
            ChatMessageCreate(
                role="system",
                content=content,
                tags=tags,
                ai_config_id=ctx.get("ai_config_id"),
                ai_kind=ctx.get("ai_kind") or "assistant",
                session_id=session_id,
                session_name=ctx.get("session_name"),
                model=ctx.get("model"),
                total_tokens=0,
            ),
        )


_SCREENSHOT_TOOLS = {"browser_screenshot", "screen.capture", "screen.capture_region", "vision.capture", "vision.capture_mouse"}
_IMAGE_DATA_URL_KEYS = {"dataUrl", "data_url", "imageDataUrl", "screenshotDataUrl", "screenshot"}


def _explicit_send_disabled(args: Any) -> bool:
    if not isinstance(args, dict):
        return False
    return any(
        key in args and args.get(key) is False
        for key in ("send_to_user", "bot_send_to_user", "deliver_to_user")
    )


def _should_send_screenshot_to_user(tool: str, result: Any, args: Any = None) -> bool:
    if _explicit_send_disabled(args):
        return False
    return (
        (isinstance(result, dict) and (
            result.get("send_to_user") is True
            or result.get("bot_send_to_user") is True
            or result.get("deliver_to_user") is True
        ))
        or str(tool or "") in {"vision.capture", "vision.capture_mouse", "screen.capture", "screen.capture_region"}
    )


def _normalize_screenshot_result_for_delivery(tool: str, result: Any, args: Any = None) -> Any:
    if not isinstance(result, dict):
        return result
    if not _should_send_screenshot_to_user(tool, result, args):
        return result
    next_result = dict(result)
    next_result["send_to_user"] = True
    next_result["save_to_server"] = True
    return next_result


def _omit_screenshot_bytes(value: Any) -> Any:
    if isinstance(value, dict):
        out: Dict[str, Any] = {}
        for key, item in value.items():
            if key in _IMAGE_DATA_URL_KEYS and isinstance(item, str) and item.startswith("data:image/"):
                out[key] = f"<image data URL omitted, {len(item)} chars>"
            elif key in {"server_path", "workspace_path"}:
                out[key] = item
            else:
                out[key] = _omit_screenshot_bytes(item)
        return out
    if isinstance(value, list):
        return [_omit_screenshot_bytes(item) for item in value]
    return value


async def _emit_to_user(ctx: Dict[str, Any], event: str, payload: Dict[str, Any]) -> None:
    user_id = ctx.get("user_id")
    if user_id is None:
        return
    await sio.emit(event, payload, room=f"user_{user_id}")


async def handle_task_progress(data: Dict[str, Any]) -> None:
    ctx = _resolve_result_context(data)
    await _emit_to_user(ctx, "device:task_progress", {
        "taskId": data.get("taskId"),
        "deviceId": ctx.get("device_id"),
        "message": str(data.get("message") or ""),
        "updatedAt": time.time(),
    })


async def handle_task_result(data: Dict[str, Any]) -> None:
    ctx = _resolve_result_context(data)
    task_id = str(data.get("taskId") or "")
    device_id = str(ctx.get("device_id") or data.get("deviceId") or "unknown")
    success = bool(data.get("success", True))
    tool = str(data.get("tool") or ctx.get("tool") or "")
    summary = str(data.get("summary") or "")
    result = data.get("result")
    if success and tool in _SCREENSHOT_TOOLS:
        result = _normalize_screenshot_result_for_delivery(tool, result, ctx.get("args"))
        try:
            result = attach_persisted_screenshot(
                user_id=int(ctx.get("user_id") or 0),
                ai_config_id=ctx.get("ai_config_id"),
                tool=tool,
                result=result,
            )
        except Exception as exc:
            if isinstance(result, dict):
                result = dict(result)
                result["uploaded"] = False
                result["upload_error"] = str(exc)

    status = "成功" if success else "失败"
    display_result = _omit_screenshot_bytes(result) if tool in _SCREENSHOT_TOOLS else result
    result_text = result if isinstance(result, str) else _safe_dump(display_result)
    agent_label = _device_kind_label(device_id)
    content = (
        f"[{agent_label}执行结果]\n"
        f"Agent: {device_id}\n"
        f"工具: {tool or '(综合任务)'}\n"
        f"状态: {status}\n\n"
        f"[摘要]\n{summary or '(无摘要)'}\n\n"
        f"[结果]\n{result_text}"
    )
    if not bool(ctx.get("suppress_session_message")):
        _save_agent_message(ctx, content, "agent_task_result")
    _update_agent_task_state(device_id, status="success" if success else "failed", task_id=task_id)
    _finalize_dispatch_row(
        task_id,
        status="completed" if success else "error",
        success=success,
        summary=summary,
        result=result,
        error=None if success else summary or "agent reported failure",
    )
    waiter = _PENDING_DISPATCH_WAITERS.get(task_id)
    waiter_payload = {
        "success": success,
        "taskId": task_id,
        "deviceId": device_id,
        "tool": tool,
        "summary": summary,
        "result": result,
    }
    if waiter:
        loop = waiter.get("loop")
        future = waiter.get("future")
        if loop and future and not future.done():
            loop.call_soon_threadsafe(future.set_result, waiter_payload)
    await _emit_to_user(ctx, "device:task_result", {
        "taskId": task_id,
        "deviceId": device_id,
        "success": success,
        "tool": tool,
        "summary": summary,
        "result": result,
        "updatedAt": time.time(),
    })
    _PENDING_DISPATCHES.pop(task_id, None)
    await resume_device_dispatch_queue(device_id)


async def handle_task_error(data: Dict[str, Any]) -> None:
    ctx = _resolve_result_context(data)
    task_id = str(data.get("taskId") or "")
    device_id = str(ctx.get("device_id") or data.get("deviceId") or "unknown")
    error = str(data.get("error") or "Unknown agent error")
    agent_label = _device_kind_label(device_id)
    content = (
        f"[{agent_label}执行失败]\n"
        f"Agent: {device_id}\n"
        f"工具: {ctx.get('tool') or '(综合任务)'}\n\n"
        f"[错误]\n{error}"
    )
    if not bool(ctx.get("suppress_session_message")):
        _save_agent_message(ctx, content, "agent_task_error")
    _update_agent_task_state(device_id, status="error", task_id=task_id, error=error)
    _finalize_dispatch_row(
        task_id,
        status="error",
        success=False,
        error=error,
    )
    waiter = _PENDING_DISPATCH_WAITERS.get(task_id)
    if waiter:
        loop = waiter.get("loop")
        future = waiter.get("future")
        if loop and future and not future.done():
            loop.call_soon_threadsafe(future.set_result, {
                "success": False,
                "taskId": task_id,
                "deviceId": device_id,
                "tool": str(ctx.get("tool") or ""),
                "error": error,
                "result": None,
            })
    await _emit_to_user(ctx, "device:task_error", {
        "taskId": task_id,
        "deviceId": device_id,
        "error": error,
        "updatedAt": time.time(),
    })
    _PENDING_DISPATCHES.pop(task_id, None)
    await resume_device_dispatch_queue(device_id)


def _safe_dump(value: Any) -> str:
    import json
    try:
        return json.dumps(value, ensure_ascii=False, indent=2)
    except Exception:
        return str(value)


async def _execute_workshop_inline(
    *,
    user_id: int,
    ai_config_id: Optional[int],
    tool: str,
    args: Optional[Dict[str, Any]],
) -> Dict[str, Any]:
    """知识与进化工坊是服务端内置的，无 socket 往返：直接进程内执行
    （policy 钩子 + 服务端复核见 ``library.engine.execute_tool``）。"""
    from fastapi import HTTPException

    from library import engine as workshop_engine

    device_id = workshop_engine.device_id_for_user(user_id)
    try:
        result = await asyncio.to_thread(
            workshop_engine.execute_tool, user_id, ai_config_id, tool, dict(args or {})
        )
        return {"success": True, "deviceId": device_id, "tool": tool, "summary": "", "result": result}
    except HTTPException as exc:
        return {"success": False, "deviceId": device_id, "tool": tool, "error": str(exc.detail)}
    except Exception as exc:
        logger.exception("workshop tool failed tool=%s user=%s", tool, user_id)
        return {"success": False, "deviceId": device_id, "tool": tool, "error": str(exc)}


async def dispatch_endpoint_tool(
    *,
    user_id: int,
    ai_config_id: Optional[int],
    tool: str,
    args: Optional[Dict[str, Any]] = None,
) -> Optional[str]:
    """Fire-and-forget variant of :func:`dispatch_endpoint_tool_and_wait`.

    Emits ``task:dispatch`` to the right agent and returns the ``task_id``
    immediately. The dispatch row is persisted before the emit so the
    caller can poll ``AgentDispatchTask`` by ``task_id`` and survive a
    connector-runtime restart. Returns ``None`` when no agent is bound.

    工坊工具没有异步往返：内联执行后落一条已完成的 dispatch 行，
    轮询方拿到的直接是终态。
    """
    tool_name = str(tool or "").strip()
    if not is_endpoint_agent_tool(tool_name) or not ai_config_id:
        return None

    if is_workshop_tool(tool_name):
        from library import engine as workshop_engine

        task_id = f"atask_{uuid.uuid4().hex[:12]}"
        run_ctx = get_run_session_context() or {}
        _persist_dispatch(
            task_id=task_id,
            user_id=user_id,
            ai_config_id=ai_config_id,
            ai_kind=str(run_ctx.get("ai_kind") or "assistant"),
            session_id=str(run_ctx.get("session_id") or ""),
            session_name=run_ctx.get("session_name"),
            device_id=workshop_engine.device_id_for_user(user_id),
            tool=tool_name,
            instruction=f"Run workshop MCP tool {tool_name}",
        )
        outcome = await _execute_workshop_inline(
            user_id=user_id, ai_config_id=ai_config_id, tool=tool_name, args=args
        )
        _finalize_dispatch_row(
            task_id,
            status="completed" if outcome.get("success") else "error",
            success=bool(outcome.get("success")),
            summary="",
            result=outcome.get("result"),
            error=outcome.get("error"),
        )
        return task_id

    if is_browser_tool(tool_name):
        agent = get_connected_browser_agent(ai_config_id, user_id)
    elif is_desktop_tool(tool_name):
        agent = get_connected_desktop_agent(ai_config_id, user_id)
    else:
        agent = None
    if not agent:
        return None
    device_id = str(agent.get("id") or "").strip()
    if not device_id:
        return None

    run_ctx = get_run_session_context() or {}
    result = await dispatch_task_to_agent(
        device_id=device_id,
        user_id=user_id,
        ai_config_id=ai_config_id,
        ai_kind=str(run_ctx.get("ai_kind") or "assistant"),
        session_id=str(run_ctx.get("session_id") or ""),
        session_name=run_ctx.get("session_name"),
        model=run_ctx.get("model"),
        instruction=f"Run endpoint MCP tool {tool_name}",
        tool=tool_name,
        args=args or {},
        allowed_tools=[tool_name],
        wait_for_result=False,
        suppress_session_message=True,
    )
    return str(result.get("taskId") or "") or None


async def dispatch_endpoint_tool_and_wait(
    *,
    user_id: int,
    ai_config_id: Optional[int],
    tool: str,
    args: Optional[Dict[str, Any]] = None,
    timeout_seconds: int = 120,
) -> Dict[str, Any]:
    tool_name = str(tool or "").strip()
    if not is_endpoint_agent_tool(tool_name):
        return {"success": False, "error": f"Not an endpoint agent tool: {tool_name}"}
    if not ai_config_id:
        return {"success": False, "error": "ai_config_id is required for endpoint MCP tools"}

    # 知识与进化工坊：服务端内置，进程内直接执行（无 socket 往返）。
    if is_workshop_tool(tool_name):
        return await _execute_workshop_inline(
            user_id=user_id, ai_config_id=ai_config_id, tool=tool_name, args=args
        )

    agent = None
    if is_browser_tool(tool_name):
        agent = get_connected_browser_agent(ai_config_id, user_id)
    elif is_desktop_tool(tool_name):
        agent = get_connected_desktop_agent(ai_config_id, user_id)
    if not agent:
        kind = "browser" if is_browser_tool(tool_name) else "desktop"
        return {"success": False, "error": f"No connected {kind} agent bound to ai_config_id={ai_config_id}"}

    device_id = str(agent.get("id") or "").strip()
    if not device_id:
        return {"success": False, "error": "Connected endpoint agent has no id"}

    effective_timeout_seconds = _endpoint_timeout_seconds(
        tool_name,
        args or {},
        timeout_seconds,
    )
    run_ctx = get_run_session_context() or {}
    return await dispatch_task_to_agent(
        device_id=device_id,
        user_id=user_id,
        ai_config_id=ai_config_id,
        ai_kind=str(run_ctx.get("ai_kind") or "assistant"),
        session_id=str(run_ctx.get("session_id") or ""),
        session_name=run_ctx.get("session_name"),
        model=run_ctx.get("model"),
        instruction=f"Run endpoint MCP tool {tool_name}",
        tool=tool_name,
        args=args or {},
        allowed_tools=[tool_name],
        wait_for_result=True,
        timeout_seconds=effective_timeout_seconds,
        suppress_session_message=True,
    )


def _endpoint_timeout_seconds(tool: str, args: Dict[str, Any], fallback: int) -> int:
    """Resolve server-side wait timeout from endpoint tool args.

    Screenshot pages can wedge inside Chrome or lose a large socket payload.
    Tool-level timeouts inside the browser extension do not help if the
    extension never replies, so the dispatch waiter must honor timeout args too.
    """
    candidates = [
        args.get("timeout_seconds"),
        (float(args["task_timeout_ms"]) / 1000.0) if args.get("task_timeout_ms") is not None else None,
        (float(args["timeout_ms"]) / 1000.0) if args.get("timeout_ms") is not None else None,
    ]
    for value in candidates:
        try:
            parsed = int(float(value))
        except (TypeError, ValueError):
            continue
        if parsed > 0:
            return max(1, min(300, parsed))
    if str(tool or "") == "browser_screenshot":
        return max(1, min(60, int(fallback or 35)))
    return max(1, int(fallback or 120))


