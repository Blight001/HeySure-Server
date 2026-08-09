"""Progress, result, and error handling for endpoint dispatches."""

import json
import logging
import time
from dataclasses import dataclass
from typing import Any, Dict

from sqlmodel import Session, select

from api.models import AgentDispatchTask, ChatMessageCreate
from api.services.chat.chat_persistence import _save_message
from api.services.mcp.mcp_tool_media import canonical_screenshot_tool_name
from api.services.storage.screenshot_store import attach_persisted_screenshot
from api.sio import sio
from connector_runtime.dispatch import repository
from connector_runtime.dispatch.result_payloads import (
    normalize_screenshot_result_for_delivery,
    omit_screenshot_bytes,
    persist_cookies_result,
)


logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class ResultEnvelope:
    task_id: str
    context: Dict[str, Any]
    device_id: str
    success: bool
    tool: str
    summary: str
    result: Any
    screenshot: bool


async def handle_task_progress(data: Dict[str, Any]) -> None:
    context = _resolve_result_context(data)
    task_id = str(data.get("taskId") or "")
    _renew_progress_lease(task_id)
    await _emit_to_user(context, "device:task_progress", {
        "taskId": data.get("taskId"),
        "deviceId": context.get("device_id"),
        "sessionId": context.get("session_id"),
        "aiConfigId": context.get("ai_config_id"),
        "aiKind": context.get("ai_kind") or "assistant",
        "tool": context.get("tool") or data.get("tool") or "",
        "progress": data.get("progress"),
        "message": str(data.get("message") or ""),
        "updatedAt": time.time(),
    })


async def handle_task_result(data: Dict[str, Any]) -> bool:
    task_id = str(data.get("taskId") or "")
    if _recorded_outcome(task_id):
        _audit_duplicate(task_id, "duplicate_terminal_result")
        return False
    envelope = _prepare_result(data)
    _persist_result(envelope)
    _apply_workflow_result(
        envelope.task_id,
        envelope.success,
        envelope.result,
        None if envelope.success else envelope.summary or "agent reported failure",
    )
    waiter_payload = {
        "success": envelope.success,
        "taskId": envelope.task_id,
        "deviceId": envelope.device_id,
        "tool": envelope.tool,
        "summary": envelope.summary,
        "result": envelope.result,
    }
    _release_waiter(envelope.task_id, waiter_payload)
    await _emit_to_user(envelope.context, "device:task_result", {
        **waiter_payload,
        "sessionId": envelope.context.get("session_id"),
        "aiConfigId": envelope.context.get("ai_config_id"),
        "aiKind": envelope.context.get("ai_kind") or "assistant",
        "updatedAt": time.time(),
    })
    await _complete_dispatch(envelope.task_id, envelope.device_id)
    return True


async def handle_task_error(data: Dict[str, Any]) -> bool:
    task_id = str(data.get("taskId") or "")
    if _recorded_outcome(task_id):
        _audit_duplicate(task_id, "duplicate_terminal_error")
        return False
    dispatch = _dispatch_module()
    context = _resolve_result_context(data)
    device_id = str(context.get("device_id") or data.get("deviceId") or "unknown")
    error = str(data.get("error") or "Unknown agent error")
    label = dispatch._device_kind_label(device_id)
    if not bool(context.get("suppress_session_message")):
        _save_agent_message(
            context,
            f"[{label}执行失败]\n端侧: {label}\n工具: {context.get('tool') or '(综合任务)'}"
            f"\n\n[错误]\n{error}",
            "agent_task_error",
        )
    dispatch._update_agent_task_state(
        device_id, status="error", task_id=task_id, error=error
    )
    repository.finalize_dispatch_row(
        task_id, status="error", success=False, error=error
    )
    _apply_workflow_result(task_id, False, None, error)
    payload = {
        "success": False,
        "taskId": task_id,
        "deviceId": device_id,
        "tool": str(context.get("tool") or ""),
        "error": error,
        "result": None,
    }
    _release_waiter(task_id, payload)
    await _emit_to_user(context, "device:task_error", {
        **payload,
        "sessionId": context.get("session_id"),
        "aiConfigId": context.get("ai_config_id"),
        "aiKind": context.get("ai_kind") or "assistant",
        "tool": str(context.get("tool") or data.get("tool") or ""),
        "updatedAt": time.time(),
    })
    await _complete_dispatch(task_id, device_id)
    return True


def _prepare_result(data) -> ResultEnvelope:
    context = _resolve_result_context(data)
    tool = str(data.get("tool") or context.get("tool") or "")
    success = bool(data.get("success", True))
    result = data.get("result")
    screenshot = bool(canonical_screenshot_tool_name(tool))
    if success and screenshot:
        result = _persist_screenshot(context, tool, result)
    if success and tool in ("save_cookies", "capture_cookies"):
        result = _persist_cookies(context, result)
    return ResultEnvelope(
        task_id=str(data.get("taskId") or ""),
        context=context,
        device_id=str(context.get("device_id") or data.get("deviceId") or "unknown"),
        success=success,
        tool=tool,
        summary=str(data.get("summary") or ""),
        result=result,
        screenshot=screenshot,
    )


def _persist_result(envelope: ResultEnvelope) -> None:
    dispatch = _dispatch_module()
    status = "成功" if envelope.success else "失败"
    display = omit_screenshot_bytes(envelope.result) if envelope.screenshot else envelope.result
    result_text = envelope.result if isinstance(envelope.result, str) else _safe_dump(display)
    label = dispatch._device_kind_label(envelope.device_id)
    content = (
        f"[{label}执行结果]\n端侧: {label}\n工具: {envelope.tool or '(综合任务)'}\n"
        f"状态: {status}\n\n[摘要]\n{envelope.summary or '(无摘要)'}\n\n[结果]\n{result_text}"
    )
    if not bool(envelope.context.get("suppress_session_message")):
        _save_agent_message(envelope.context, content, "agent_task_result")
    dispatch._update_agent_task_state(
        envelope.device_id,
        status="success" if envelope.success else "failed",
        task_id=envelope.task_id,
    )
    repository.finalize_dispatch_row(
        envelope.task_id,
        status="completed" if envelope.success else "error",
        success=envelope.success,
        summary=envelope.summary,
        result=envelope.result,
        error=None if envelope.success else envelope.summary or "agent reported failure",
    )


def _persist_screenshot(context, tool, result):
    normalized = normalize_screenshot_result_for_delivery(
        tool, result, context.get("args")
    )
    try:
        return attach_persisted_screenshot(
            user_id=int(context.get("user_id") or 0),
            ai_config_id=context.get("ai_config_id"),
            tool=tool,
            result=normalized,
        )
    except Exception as exc:
        if isinstance(normalized, dict):
            normalized = dict(normalized)
            normalized["uploaded"] = False
            normalized["upload_error"] = str(exc)
        return normalized


def _persist_cookies(context, result):
    try:
        return persist_cookies_result(
            user_id=int(context.get("user_id") or 0),
            ai_config_id=context.get("ai_config_id"),
            result=result,
        )
    except Exception as exc:
        logger.exception("cookie persist wrapper failed")
        if isinstance(result, dict):
            result = dict(result)
            result["saved_to_server"] = False
            result["save_error"] = str(exc)
        return result


def _resolve_result_context(data):
    dispatch = _dispatch_module()
    task_id = str(data.get("taskId") or "")
    context = dispatch._PENDING_DISPATCHES.get(task_id)
    if context:
        return context
    if task_id:
        try:
            with Session(repository.engine) as session:
                row = session.exec(
                    select(AgentDispatchTask).where(AgentDispatchTask.task_id == task_id)
                ).first()
            if row:
                return dispatch._context_from_dispatch_row(row)
        except Exception:
            logger.exception("result context lookup failed task=%s", task_id)
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


def _renew_progress_lease(task_id: str) -> None:
    if not task_id:
        return
    try:
        with Session(repository.engine) as session:
            row = session.exec(
                select(AgentDispatchTask).where(AgentDispatchTask.task_id == task_id)
            ).first()
            if row and row.status == "pending" and row.owner_instance_id == repository.CONNECTOR_INSTANCE_ID:
                row.updated_at = time.time()
                row.lease_expires_at = repository.lease_deadline()
                session.add(row)
                session.commit()
    except Exception:
        logger.exception("failed to renew dispatch lease task=%s", task_id)


def _save_agent_message(context, content, tags) -> None:
    user_id = context.get("user_id")
    session_id = context.get("session_id")
    if not user_id or not session_id:
        return
    with Session(repository.engine) as session:
        _save_message(
            session,
            int(user_id),
            ChatMessageCreate(
                role="system", content=content, tags=tags,
                ai_config_id=context.get("ai_config_id"),
                ai_kind=context.get("ai_kind") or "assistant",
                session_id=session_id, session_name=context.get("session_name"),
                model=context.get("model"), total_tokens=0,
            ),
        )


async def _emit_to_user(context, event, payload) -> None:
    user_id = context.get("user_id")
    if user_id is not None:
        await sio.emit(event, payload, room=f"user_{user_id}")


def _recorded_outcome(task_id):
    return _dispatch_module().dispatch_has_recorded_outcome(task_id)


def _audit_duplicate(task_id, reason) -> None:
    try:
        from api.services.workflows.run_service import record_ignored_step_result

        with Session(repository.engine) as session:
            record_ignored_step_result(session, task_id, reason=reason)
    except Exception:
        logger.exception("failed to audit duplicate workflow outcome task=%s", task_id)


def _apply_workflow_result(task_id, success, result, error) -> None:
    try:
        from api.core.settings import settings

        if not settings.workflow_scheduler_enabled:
            return
        from api.services.workflows.run_service import apply_step_result

        with Session(repository.engine) as session:
            apply_step_result(
                session,
                dispatch_task_id=task_id,
                success=success,
                result=result,
                error=error,
            )
    except Exception:
        logger.exception("workflow result hook failed task=%s", task_id)


def _release_waiter(task_id, payload) -> None:
    waiter = _dispatch_module()._PENDING_DISPATCH_WAITERS.get(task_id)
    if not waiter:
        return
    loop = waiter.get("loop")
    future = waiter.get("future")
    if loop and future and not future.done():
        loop.call_soon_threadsafe(future.set_result, payload)


async def _complete_dispatch(task_id, device_id) -> None:
    dispatch = _dispatch_module()
    dispatch._PENDING_DISPATCHES.pop(task_id, None)
    await dispatch.resume_device_dispatch_queue(device_id)


def _safe_dump(value) -> str:
    try:
        return json.dumps(value, ensure_ascii=False, indent=2)
    except Exception:
        return str(value)


def _dispatch_module():
    from connector_runtime.dispatch import device_dispatch

    return device_dispatch
