"""Socket task progress/result/error handlers."""

from __future__ import annotations

from typing import Any, Dict
from pydantic import ValidationError

from api.devices.live import emit_agent_list_for_user
from api.sio import agents
from api.runtime.log_context import bind as bind_log_context
from connector_runtime.dispatch.device_dispatch import (
    handle_task_error,
    handle_task_progress,
    handle_task_result,
)
from connector_runtime.socket_handlers.schemas import (
    TaskErrorPayload,
    TaskProgressPayload,
    TaskResultPayload,
    validated_payload,
)


async def progress(_sid: str, data: object) -> None:
    try:
        payload = validated_payload(TaskProgressPayload, data)
    except ValidationError:
        return
    with bind_log_context(
        task_id=payload.get("taskId"), device_id=payload.get("deviceId"), stage="progress"
    ):
        await handle_task_progress(payload)


async def _finish(sid: str, data: object, schema, handler) -> Dict[str, Any]:
    try:
        payload = validated_payload(schema, data)
    except ValidationError:
        return {"received": False, "error_code": "AGENT_TASK_PAYLOAD_INVALID"}
    with bind_log_context(
        task_id=payload.get("taskId"), device_id=payload.get("deviceId"), stage="result"
    ):
        processed = await handler(payload)
    user_id = (agents.get(sid) or {}).get("userId")
    if user_id is not None:
        await emit_agent_list_for_user(user_id)
    return {"received": True, "taskId": payload.get("taskId"), "duplicate": not processed}


async def result(sid: str, data: object) -> Dict[str, Any]:
    return await _finish(sid, data, TaskResultPayload, handle_task_result)


async def error(sid: str, data: object) -> Dict[str, Any]:
    return await _finish(sid, data, TaskErrorPayload, handle_task_error)
