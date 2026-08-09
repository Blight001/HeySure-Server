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

import asyncio
import json
import logging
import time
import uuid
from typing import Any, Dict, List, Optional

from sqlmodel import Session, select

from api.database import engine
from connector_runtime.dispatch.desktop_device_tools import (
    device_type_of,
    get_connected_browser_agent,
    get_connected_desktop_agent,
    is_browser_tool,
    is_desktop_tool,
    is_endpoint_agent_tool,
    is_workshop_tool,
)
from api.models import AgentDispatchTask, DeviceAiBinding, DevicePresence
from api.sio import agents, sio
from api.runtime.run_context import get_run_session_context, set_run_session_context
from connector_runtime.dispatch import models as dispatch_models, repository as dispatch_repository
from connector_runtime.dispatch import dispatch_cancellation
from connector_runtime.dispatch import dispatch_results
from connector_runtime.dispatch import workshop_dispatch


logger = logging.getLogger(__name__)
CONNECTOR_INSTANCE_ID = dispatch_repository.CONNECTOR_INSTANCE_ID
_enqueue_dispatch_row = dispatch_repository.enqueue_dispatch_row
_claim_next_queued = dispatch_repository.claim_next_queued
expire_orphan_dispatches = dispatch_repository.expire_orphan_dispatches
_finalize_dispatch_row = dispatch_repository.finalize_dispatch_row
_lease_deadline = dispatch_repository.lease_deadline
_persist_dispatch = dispatch_repository.persist_dispatch
_requeue_pending = dispatch_repository.requeue_pending
TERMINAL_DISPATCH_STATUSES = dispatch_models.TERMINAL_DISPATCH_STATUSES

expire_dispatch = dispatch_cancellation.expire_dispatch
cancel_dispatch = dispatch_cancellation.cancel_dispatch

# Per-run session context so MCP tools (running inside the worker thread) can
# attach dispatched-task results to the correct chat session. asyncio.run()
# copies the current context, so a value set before the tool call is visible
# inside the (async) MCP handler.
# taskId -> dispatch context (for routing results back to a session).
_PENDING_DISPATCHES: Dict[str, Dict[str, Any]] = {}
_PENDING_DISPATCH_WAITERS: Dict[str, Dict[str, Any]] = {}

# Dispatches with no agent reply after this many seconds are considered lost and
# are dropped so the in-memory map does not grow unbounded when an agent drops.
_DISPATCH_TTL_SECONDS = 1800


def dispatch_has_recorded_outcome(task_id: str) -> bool:
    """Return whether a real agent outcome for ``task_id`` is already stored.

    Result delivery is ACK/retry based. If the server committed a result but
    its Socket.IO ACK was lost, the agent sends the same task again. Treat that
    retry as an idempotent acknowledgement instead of writing a duplicate chat
    message or advancing the device queue twice. Timeout/cancellation are also
    immutable terminal decisions: a late side-effect result is audited by the
    agent logs but cannot resurrect a caller that already moved on.
    """
    if not task_id:
        return False
    try:
        with Session(engine) as session:
            row = session.exec(
                select(AgentDispatchTask).where(AgentDispatchTask.task_id == task_id)
            ).first()
            terminal = {state.value for state in TERMINAL_DISPATCH_STATUSES}
            return bool(row and row.status in terminal)
    except Exception as exc:
        logger.exception(f"recorded outcome lookup failed task={task_id}: {exc}")
        return False


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
            return "图书馆"
        if bool(agent.get("isBrowserExtension")) or "browser-extension" in platform:
            return "浏览器插件"
        if bool(agent.get("isAndroid")) or "android" in platform:
            return "安卓端"
        if bool(agent.get("isWindowsDesktop")) or "desktop" in platform:
            return "桌面端"
        if "browser" in platform:
            return "浏览器端"
        if device_type_of(agent) == "custom":
            return str(agent.get("name") or "").strip() or "自定义设备"
        return "端侧设备"
    return "端侧设备"


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


def _dispatch_record(task_id: str, ctx: Dict[str, Any]) -> dispatch_models.DispatchRecord:
    return dispatch_models.DispatchRecord(
        task_id=task_id,
        user_id=int(ctx.get("user_id") or 0),
        ai_config_id=ctx.get("ai_config_id"),
        ai_kind=str(ctx.get("ai_kind") or "assistant"),
        session_id=str(ctx.get("session_id") or ""),
        session_name=ctx.get("session_name"),
        device_id=str(ctx.get("device_id") or ""),
        tool=str(ctx.get("tool") or ""),
        instruction=str(ctx.get("instruction") or ""),
        args=ctx.get("args") if isinstance(ctx.get("args"), dict) else {},
        suppress_session_message=bool(ctx.get("suppress_session_message")),
    )


async def resume_device_dispatch_queue(device_id: str) -> Optional[str]:
    """Dispatch the oldest queued task when ``device_id`` has no active task."""
    target_sid = _find_agent_sid(device_id)
    if not target_sid:
        return None

    row = _claim_next_queued(device_id)
    if not row:
        return None
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
        _requeue_pending(task_id)
        raise
    return task_id


async def redeliver_dispatch(task_id: str) -> bool:
    """Best-effort re-emit of one persisted active dispatch.

    Workflow recovery uses this after a crash in the small window between the
    dispatch-row commit and marking its step as waiting. Endpoint agents dedupe
    by taskId, so sending the same payload is safe.
    """
    with Session(engine) as session:
        row = session.exec(
            select(AgentDispatchTask).where(AgentDispatchTask.task_id == task_id)
        ).first()
        if not row or row.status not in {"pending", "queued"}:
            return False
        if row.status == "queued":
            return True
        ctx = _context_from_dispatch_row(row)
    target_sid = _find_agent_sid(str(ctx["device_id"]))
    if not target_sid:
        return False
    _PENDING_DISPATCHES[task_id] = ctx
    await sio.emit(
        "task:dispatch",
        {
            "taskId": task_id,
            "userId": ctx["user_id"],
            "aiConfigId": ctx["ai_config_id"],
            "sessionId": ctx["session_id"],
            "instruction": ctx["instruction"],
            "tool": ctx["tool"],
            "args": ctx["args"],
            "allowedTools": [ctx["tool"]] if ctx["tool"] else [],
        },
        to=target_sid,
    )
    return True


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
    task_id: Optional[str] = None,
) -> Dict[str, Any]:
    target_sid = _find_agent_sid(device_id)

    purge_stale_dispatches()
    task_id = str(task_id or f"atask_{uuid.uuid4().hex[:12]}")
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
    dispatch_record = _dispatch_record(task_id, dispatch_ctx)
    dispatch_status = _enqueue_dispatch_row(dispatch_record, timeout_seconds=timeout_seconds)
    _PENDING_DISPATCHES[task_id] = dispatch_ctx
    waiter = None
    if wait_for_result:
        loop = asyncio.get_running_loop()
        waiter = {"loop": loop, "future": loop.create_future()}
        _PENDING_DISPATCH_WAITERS[task_id] = waiter
    if dispatch_status == "pending" and target_sid:
        try:
            await sio.emit("task:dispatch", payload, to=target_sid)
        except Exception as exc:
            _PENDING_DISPATCHES.pop(task_id, None)
            _finalize_dispatch_row(task_id, status="error", success=False, error=str(exc))
            await resume_device_dispatch_queue(device_id)
            raise
    elif dispatch_status != "pending":
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
            # is ignored after the timeout because terminal rows are immutable.
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
    public_status = dispatch_status if target_sid else "waiting_device"
    return {
        "success": True,
        "taskId": task_id,
        "deviceId": device_id,
        "status": public_status,
        "note": (
            f"Task dispatched to {_device_kind_label(device_id)}."
            if public_status == "pending"
            else f"Task is waiting for {_device_kind_label(device_id)} to reconnect."
            if public_status == "waiting_device"
            else f"Task queued for {_device_kind_label(device_id)}; it will run after the current task."
        ),
    }


handle_task_progress = dispatch_results.handle_task_progress


handle_task_result = dispatch_results.handle_task_result


handle_task_error = dispatch_results.handle_task_error


_execute_workshop_inline = workshop_dispatch.execute_workshop_inline


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
        record_ctx = {
            **run_ctx,
            "user_id": user_id,
            "ai_config_id": ai_config_id,
            "device_id": workshop_engine.device_id_for_user(user_id),
            "tool": tool_name,
            "instruction": f"Run workshop MCP tool {tool_name}",
        }
        _persist_dispatch(_dispatch_record(task_id, record_ctx))
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
        agent = get_connected_browser_agent(ai_config_id, user_id, tool=tool_name)
    elif is_desktop_tool(tool_name):
        agent = get_connected_desktop_agent(ai_config_id, user_id, tool=tool_name)
    else:
        agent = None
    device_id = str(agent.get("id") or "").strip() if agent else ""
    if not device_id:
        # Preserve bot-originated MCP calls across short endpoint reconnects.
        with Session(engine) as session:
            rows = session.exec(
                select(DeviceAiBinding, DevicePresence)
                .join(DevicePresence, DevicePresence.device_id == DeviceAiBinding.device_id)
                .where(
                    DeviceAiBinding.user_id == int(user_id),
                    DeviceAiBinding.ai_config_id == int(ai_config_id),
                    DevicePresence.user_id == int(user_id),
                )
            ).all()
        for binding, presence in rows:
            try:
                capabilities = set(json.loads(presence.capabilities_json or "[]"))
            except Exception:
                capabilities = set()
            if tool_name in capabilities:
                device_id = str(binding.device_id or "").strip()
                break
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
        agent = get_connected_browser_agent(ai_config_id, user_id, tool=tool_name)
    elif is_desktop_tool(tool_name):
        agent = get_connected_desktop_agent(ai_config_id, user_id, tool=tool_name)
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


