"""Apply successful tool metadata side effects to an inference session."""

from dataclasses import dataclass
import logging
from typing import Optional

from sqlmodel import Session

from api.services.chat import mcp_session_context
from ai_runtime.inference.tool_resolution import described_tool_entries


logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class ToolMetadataContext:
    session: Session
    user_id: int
    ai_config_id: Optional[int]
    ai_kind: str
    session_id: str
    session_name: str
    allowed_tools: frozenset[str]
    exposed_tools: set[str]


def apply_tool_metadata(
    context: ToolMetadataContext,
    tool: str,
    tool_result: dict,
    failed: bool,
) -> str:
    if failed:
        return ""
    payload = tool_result.get("result", tool_result)
    renamed = _renamed_current_session(context, tool, payload)
    if tool != "mcp.describe+tool":
        return renamed
    items, names = described_tool_entries(tool_result)
    context.exposed_tools.update(
        name for name in names if name and name in context.allowed_tools
    )
    try:
        mcp_session_context.remember_described_tools(
            context.session,
            user_id=context.user_id,
            ai_config_id=context.ai_config_id,
            ai_kind=context.ai_kind,
            session_id=context.session_id,
            session_name=context.session_name,
            described=items,
        )
    except Exception:
        logger.exception("persist described MCP tools failed")
    return renamed


def _renamed_current_session(context, tool, payload) -> str:
    if (
        tool != "conversation.manage"
        or not isinstance(payload, dict)
        or payload.get("action") != "rename"
        or str(payload.get("session_id") or "") != str(context.session_id)
    ):
        return ""
    return str(payload.get("name") or "").strip()


def apply_session_rename(saved_message, current_name: str, renamed_name: str) -> str:
    if not renamed_name:
        return current_name
    saved_message.session_name = renamed_name
    return renamed_name
