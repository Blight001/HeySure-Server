"""Durable MCP discovery state and compact history replay helpers."""

from __future__ import annotations

import json
import re
import time
from typing import Any, Dict, Iterable, List

from sqlmodel import Session, select

from api.models import ChatSession


def _session_stmt(user_id: int, ai_config_id: int | None, ai_kind: str, session_id: str):
    stmt = select(ChatSession).where(
        ChatSession.user_id == user_id,
        ChatSession.ai_kind == ai_kind,
        ChatSession.session_id == session_id,
    )
    if ai_config_id is None:
        return stmt.where(ChatSession.ai_config_id.is_(None))
    return stmt.where(ChatSession.ai_config_id == ai_config_id)


def _decode_state(raw: str) -> Dict[str, Dict[str, Any]]:
    try:
        parsed = json.loads(str(raw or "{}"))
    except Exception:
        return {}
    if not isinstance(parsed, dict):
        return {}
    return {
        str(name): value
        for name, value in parsed.items()
        if str(name).strip() and isinstance(value, dict)
    }


def described_tool_versions(
    session: Session,
    *,
    user_id: int,
    ai_config_id: int | None,
    ai_kind: str,
    session_id: str,
) -> Dict[str, str]:
    row = session.exec(_session_stmt(user_id, ai_config_id, ai_kind, session_id)).first()
    if row is None:
        return {}
    state = _decode_state(row.described_tools_json)
    return {
        name: str(item.get("schema_version") or "").strip()
        for name, item in state.items()
        if str(item.get("schema_version") or "").strip()
    }


def remember_described_tools(
    session: Session,
    *,
    user_id: int,
    ai_config_id: int | None,
    ai_kind: str,
    session_id: str,
    session_name: str,
    described: Iterable[Dict[str, Any]],
) -> None:
    additions = {
        str(item.get("name") or "").strip(): str(item.get("schemaVersion") or "").strip()
        for item in described
        if isinstance(item, dict)
        and str(item.get("name") or "").strip()
        and str(item.get("schemaVersion") or "").strip()
    }
    if not additions:
        return
    row = session.exec(_session_stmt(user_id, ai_config_id, ai_kind, session_id)).first()
    now = time.time()
    if row is None:
        row = ChatSession(
            user_id=user_id,
            ai_config_id=ai_config_id,
            ai_kind=ai_kind,
            session_id=session_id,
            session_name=session_name or session_id,
        )
    state = _decode_state(row.described_tools_json)
    for name, version in additions.items():
        state[name] = {"schema_version": version, "described_at": now}
    row.described_tools_json = json.dumps(state, ensure_ascii=False, separators=(",", ":"))
    row.updated_at = now
    session.add(row)
    session.commit()


def parse_mcp_tool_bubble(content: str) -> Dict[str, Any] | None:
    """Parse the stable UI bubble without modifying the persisted original."""
    text = str(content or "")
    prefix = "[MCP工具]\n工具: "
    if not text.startswith(prefix):
        return None
    try:
        tool_line, remainder = text[len(prefix):].split("\n", 1)
        status_part, remainder = remainder.split("\n\n[参数]\n", 1)
        arguments_text, result = remainder.split("\n\n[结果]\n", 1)
    except ValueError:
        return None
    if "\n\n[截图]\n" in result:
        result = result.split("\n\n[截图]\n", 1)[0]
    try:
        arguments = json.loads(arguments_text)
    except Exception:
        arguments = {}
    return {
        "tool": tool_line.strip(),
        "status": status_part.removeprefix("状态:").strip(),
        "arguments": arguments if isinstance(arguments, dict) else {},
        "result": result,
    }


def compact_mcp_history_messages(message_id: int | None, content: str, max_result_chars: int) -> List[Dict[str, Any]]:
    """Return a valid native tool-call pair with only its result body shortened."""
    parsed = parse_mcp_tool_bubble(content)
    if not parsed or not parsed["tool"]:
        return []
    raw_limit = int(max_result_chars or 0)
    limit = max(20, min(10000, raw_limit)) if raw_limit > 0 else 0
    full_result = str(parsed.get("result") or "")
    truncated = bool(limit and len(full_result) > limit)
    excerpt = full_result[:limit] + "…[历史返回已缩减]" if truncated else full_result
    call_id = f"history_mcp_{int(message_id or 0)}"
    native_name = re.sub(r"[^a-zA-Z0-9_-]", "__", str(parsed["tool"])).strip("_")[:64] or "tool"
    return [
        {
            "role": "assistant",
            "content": "",
            "tool_calls": [{
                "id": call_id,
                "type": "function",
                "function": {
                    "name": native_name,
                    "arguments": json.dumps(parsed["arguments"], ensure_ascii=False, separators=(",", ":")),
                },
            }],
        },
        {
            "role": "tool",
            "tool_call_id": call_id,
            "content": excerpt,
        },
    ]
