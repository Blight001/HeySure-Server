"""Stable inference worker entrypoint and documented compatibility exports."""

IS_ROUTER_ENTRY = False

from typing import Optional

from api.chat_runtime.chat_runtime_helpers import _run_set_status
from api.database import engine
from sqlmodel import Session
from ai_runtime.inference import (
    model_gateway,
    worker_lifecycle,
    worker_run_flow,
    worker_tool_batch_flow,
)
from ai_runtime.inference.policies import (
    can_start_inference_step as _can_start_inference_step,
    has_active_todo_plan as _has_active_todo_plan,
)
from ai_runtime.inference.run_request import WorkerRequest, start_worker_run
from ai_runtime.inference.tool_resolution import (
    resolve_mcp_tool_name as _resolve_mcp_tool_name,
)


_raise_for_upstream_error = model_gateway.raise_for_upstream_error
_duplicate_call_flags = worker_tool_batch_flow.duplicate_call_flags


def _run_worker(
    *,
    run_id: str,
    user_id: int,
    ai_config_id: Optional[int],
    ai_kind: str,
    session_id: str,
    session_name: str,
    model_user_content: Optional[str] = None,
    merged_system_prompt: Optional[str] = None,
    max_steps: Optional[int] = None,
    current_user_message_id: Optional[int] = None,
    selected_mcp_tools: Optional[set[str]] = None,
):
    worker_lifecycle.run_worker(
        WorkerRequest.create(
            run_id=run_id,
            user_id=user_id,
            ai_config_id=ai_config_id,
            ai_kind=ai_kind,
            session_id=session_id,
            session_name=session_name,
            model_user_content=model_user_content,
            merged_system_prompt=merged_system_prompt,
            max_steps=max_steps,
            current_user_message_id=current_user_message_id,
            selected_mcp_tools=selected_mcp_tools,
        ),
        _run_worker_impl,
    )


def _run_worker_impl(request: WorkerRequest):
    if not start_worker_run(request):
        return
    try:
        with Session(engine) as session:
            worker_run_flow.WorkerRunMachine.create(session, request).run()
    except Exception as exc:
        _run_set_status(request.run_id, "error", str(exc), finished=True)
