"""Ingest step-boundary messages and select the model-visible tool surface."""

from dataclasses import dataclass
import logging
from typing import Dict, List, Optional

from sqlmodel import Session, select

from api.models import AssistantAIConfig, ChatMessageCreate
from api.services.chat import chat_inject
from api.services.chat.chat_persistence import _save_message
from ai_runtime.inference import ai_message_service
from ai_runtime.inference.communication_prompt import (
    AIMessagePrompt,
    normalize_ai_message_type,
    render_ai_message_system_prompt,
)
from ai_runtime.inference.tool_resolution import build_native_tools_payload
from mcp_runtime.mcp.core import MCP_INTROSPECTION_TOOLS


logger = logging.getLogger(__name__)
_PRE_PLAN_KNOWLEDGE_TOOLS = {
    "knowledge.search",
    "librarian.consult",
    "librarian.list_topics",
}


@dataclass(frozen=True)
class StepMessageContext:
    session: Session
    conversation: List[Dict]
    user_id: int
    ai_config_id: Optional[int]
    ai_kind: str
    session_id: str
    session_name: str
    model: str


@dataclass(frozen=True)
class ToolExposureRequest:
    mcp_active: bool
    exposed_tools: frozenset[str]
    allowed_tools: frozenset[str]
    task_runtime: bool
    plan_active: bool
    awaiting_finish: bool
    tool_protocol: str


@dataclass(frozen=True)
class ToolExposure:
    current_tools: frozenset[str]
    provider_tools: List[Dict]
    native_name_map: Dict[str, str]


def ingest_step_messages(
    context: StepMessageContext,
    pending_reply_message_id: str,
) -> str:
    pending_reply = _ingest_ai_message(context, pending_reply_message_id)
    _ingest_user_messages(context)
    return pending_reply


def _ingest_ai_message(context, pending_reply_message_id) -> str:
    if context.ai_config_id is None:
        return pending_reply_message_id
    try:
        inbound = ai_message_service.pop_pending_for(
            context.user_id,
            int(context.ai_config_id),
            context.session_id,
        )
    except Exception:
        logger.exception("inbox poll failed")
        return pending_reply_message_id
    if inbound is None:
        return pending_reply_message_id
    requires_reply = bool(getattr(inbound, "require_reply", True))
    message_type = normalize_ai_message_type(
        getattr(inbound, "message_type", None),
        requires_reply,
    )
    injected = render_ai_message_system_prompt(AIMessagePrompt(
        from_ai_name=_resolve_ai_name(context.session, inbound.from_ai_config_id),
        from_ai_config_id=inbound.from_ai_config_id,
        target_ai_name=_resolve_ai_name(context.session, context.ai_config_id),
        target_ai_config_id=context.ai_config_id,
        message_id=inbound.message_id,
        current_session_id=context.session_id,
        content=inbound.content,
        message_type=message_type,
        require_reply=requires_reply,
    ))
    _save_message(
        context.session,
        context.user_id,
        ChatMessageCreate(
            role="user", content=injected,
            tags=f"ai_message_inbound:{message_type}:{inbound.message_id}",
            ai_config_id=context.ai_config_id, ai_kind=context.ai_kind,
            session_id=context.session_id, session_name=context.session_name,
            model=context.model, total_tokens=0,
        ),
    )
    context.conversation.append({"role": "user", "content": injected})
    return (
        str(inbound.message_id or "").strip()
        if requires_reply or message_type == "inquiry"
        else ""
    )


def _resolve_ai_name(session: Session, ai_config_id: Optional[int]) -> str:
    if not ai_config_id:
        return ""
    try:
        row = session.exec(
            select(AssistantAIConfig).where(AssistantAIConfig.id == int(ai_config_id))
        ).first()
        return str(row.name or "") if row else f"AI-{ai_config_id}"
    except Exception:
        return f"AI-{ai_config_id}"


def _ingest_user_messages(context) -> None:
    try:
        messages = chat_inject.pop_pending_injects(
            context.user_id,
            context.ai_config_id,
            context.ai_kind,
            context.session_id,
        )
    except Exception:
        logger.exception("pending user-inject poll failed")
        messages = []
    context.conversation.extend(
        {"role": "user", "content": text}
        for text in messages
    )


def select_tool_exposure(request: ToolExposureRequest) -> ToolExposure:
    if not request.mcp_active:
        return ToolExposure(frozenset(), [], {})
    allowed = set(request.allowed_tools)
    current = set(request.exposed_tools) & allowed
    current.update(set(MCP_INTROSPECTION_TOOLS) & allowed)
    if request.task_runtime and not request.plan_active:
        current.update((
            {"todo.manage"}
            | set(MCP_INTROSPECTION_TOOLS)
            | _PRE_PLAN_KNOWLEDGE_TOOLS
        ) & allowed)
    # ``awaiting_finish`` is workflow state, not an authorization boundary.
    # The explicit plan state machine decides whether another tool call may run;
    # hiding schemas here would make the model infer a false permission change.
    if request.tool_protocol == "text":
        return ToolExposure(frozenset(current), [], {})
    provider_tools, native_map = build_native_tools_payload(current)
    return ToolExposure(frozenset(current), provider_tools, native_map)
