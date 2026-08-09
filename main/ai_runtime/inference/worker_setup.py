"""Build the immutable inputs required by an inference worker run."""

from dataclasses import dataclass
from typing import Any, Dict, List

from sqlmodel import Session, select

from api.chat_runtime.chat_runtime_helpers import (
    _load_task_job_by_session,
    _load_task_payload_by_session,
    _resolve_ai_runtime,
    build_runtime_system_prompt_and_tools,
)
from api.core.config import DEFAULT_CHAT_MAX_STEPS
from api.models import ChatMessage, User
from api.services.tasks.task_system import normalize_system_auto_control
from ai_runtime.inference.conversation_history import build_conversation_history
from ai_runtime.inference.policies import coerce_max_steps
from ai_runtime.inference.run_request import WorkerRequest


@dataclass(frozen=True)
class WorkerSetup:
    user: User
    max_steps: int
    config: Any
    api_key: str
    base_url: str
    model: str
    system_prompt: str
    warning_template: str
    auto_control: Dict[str, Any]
    task_job: Any
    is_task_runtime: bool
    effective_tool_allowlist: frozenset[str]
    history: List[ChatMessage]
    conversation: List[Dict]


def prepare_worker(session: Session, request: WorkerRequest) -> WorkerSetup:
    user = session.get(User, request.user_id)
    if not user:
        raise RuntimeError("User not found")
    max_steps = coerce_max_steps(
        request.max_steps,
        coerce_max_steps(
            getattr(user, "mcp_max_steps", DEFAULT_CHAT_MAX_STEPS),
            DEFAULT_CHAT_MAX_STEPS,
        ),
    )
    from api.services.knowledge import kb_store

    config, api_key, base_url, model, system_prompt = _resolve_ai_runtime(
        session,
        user,
        request.ai_kind,
        request.ai_config_id,
        request.session_id,
    )
    warning_template = kb_store.effective_system_value(
        request.user_id,
        "mcp_format_error_hint",
        getattr(user, "mcp_format_error_hint", ""),
    ).strip()
    auto_control = normalize_system_auto_control(
        kb_store.effective_auto_control_json(request.user_id, config)
        if config
        else None
    )
    task_payload = _load_task_payload_by_session(
        session,
        request.user_id,
        request.ai_config_id,
        request.session_id,
    )
    task_job = _load_task_job_by_session(
        session,
        request.user_id,
        request.ai_config_id,
        request.session_id,
    )
    is_task_runtime = bool(task_payload) or request.session_id.startswith("session_task_")
    system_prompt, tool_allowlist = build_runtime_system_prompt_and_tools(
        session,
        user,
        ai_kind=request.ai_kind,
        ai_config_id=request.ai_config_id,
        session_id=request.session_id,
        merged_system_prompt=request.merged_system_prompt,
        cfg=config,
        base_system_prompt=system_prompt,
        task_payload=task_payload,
        selected_mcp_tools=request.selected_mcp_tools,
    )
    history, conversation = _build_history(session, request, user, system_prompt)
    return WorkerSetup(
        user=user,
        max_steps=max_steps,
        config=config,
        api_key=api_key,
        base_url=base_url,
        model=model,
        system_prompt=system_prompt,
        warning_template=warning_template,
        auto_control=auto_control,
        task_job=task_job,
        is_task_runtime=is_task_runtime,
        effective_tool_allowlist=frozenset(tool_allowlist),
        history=history,
        conversation=conversation,
    )


def _history_statement(request: WorkerRequest):
    statement = select(ChatMessage).where(
        ChatMessage.user_id == request.user_id,
        ChatMessage.session_id == request.session_id,
        ChatMessage.ai_kind == request.ai_kind,
    ).order_by(ChatMessage.created_at.asc())
    if request.ai_config_id is not None:
        statement = statement.where(ChatMessage.ai_config_id == request.ai_config_id)
    return statement


def _build_history(session, request, user, system_prompt):
    history = session.exec(_history_statement(request)).all()
    max_result_chars = max(
        20,
        min(10000, int(getattr(user, "mcp_history_result_max_chars", 8000) or 8000)),
    )
    conversation = build_conversation_history(
        history,
        system_prompt=system_prompt,
        mcp_result_max_chars=max_result_chars,
        model_user_content=request.model_user_content,
    )
    return history, conversation
