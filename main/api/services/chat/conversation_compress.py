"""Automatic conversation compression for digital-member sessions.

When a digital member's session token count reaches its threshold, the runtime
calls :func:`compress_session` to summarize the older part of the conversation
into a compact summary and CONTINUE the same session (no new generation, no
agent death, no Valhalla records).

The older messages are folded into a single ``conversation_summary`` message and
tagged ``compressed_away`` so the runtime excludes them from the model context on
subsequent turns, while the most recent few messages are kept verbatim.
"""

import json
import logging
from typing import Any, Dict, List, Optional

import requests
from sqlmodel import Session, select

from api.runtime.http_client import ai_http_post
from ...models import ChatMessage, ChatMessageCreate
from ...models.defaults import DEFAULT_COMPRESSION_PROMPT
from .chat_persistence import _save_message

logger = logging.getLogger(__name__)

# Truncate any single message body to a sane length when building the history
# text, so one runaway message cannot blow up the compression prompt.
_MAX_MSG_CHARS = 4000

_ROLE_LABELS = {"user": "用户", "assistant": "助手", "system": "系统"}


def _response_debug(resp: requests.Response, *, max_body: int = 1200) -> str:
    status = f"HTTP {getattr(resp, 'status_code', '?')}"
    reason = str(getattr(resp, "reason", "") or "").strip()
    if reason:
        status = f"{status} {reason}"
    content_type = str(getattr(resp, "headers", {}).get("content-type", "") or "").strip()
    body = str(getattr(resp, "text", "") or "").strip()
    if len(body) > max_body:
        body = body[:max_body] + "...<truncated>"
    return f"{status}; content-type={content_type or '-'}; body={body or '<empty>'}"


def _extract_sse_summary(text: str) -> str:
    parts: List[str] = []
    for raw_line in str(text or "").splitlines():
        line = raw_line.strip()
        if not line.startswith("data:"):
            continue
        payload = line[5:].strip()
        if not payload or payload == "[DONE]":
            continue
        try:
            item = json.loads(payload)
        except Exception:
            continue
        if not isinstance(item, dict):
            continue
        choices = item.get("choices")
        if not isinstance(choices, list):
            continue
        for choice in choices:
            if not isinstance(choice, dict):
                continue
            delta = choice.get("delta") if isinstance(choice.get("delta"), dict) else {}
            message = choice.get("message") if isinstance(choice.get("message"), dict) else {}
            content = delta.get("content")
            if content is None:
                content = message.get("content")
            if content:
                parts.append(str(content))
    return "".join(parts).strip()


def _extract_summary_response(resp: requests.Response) -> str:
    try:
        resp.raise_for_status()
    except Exception as exc:
        raise RuntimeError(f"summary request HTTP failure: {_response_debug(resp)}") from exc
    content_type = str(getattr(resp, "headers", {}).get("content-type", "") or "").lower()
    if "text/event-stream" in content_type:
        return _extract_sse_summary(str(getattr(resp, "text", "") or ""))
    try:
        data = resp.json()
    except Exception as exc:
        raise RuntimeError(f"summary request returned non-JSON response: {_response_debug(resp)}") from exc
    if not isinstance(data, dict):
        raise RuntimeError(f"summary request returned unexpected JSON type: {type(data).__name__}")
    choices = data.get("choices")
    if not isinstance(choices, list) or not choices:
        raise RuntimeError(f"summary request returned no choices: {_response_debug(resp)}")
    first = choices[0] if isinstance(choices[0], dict) else {}
    message = first.get("message") if isinstance(first, dict) else {}
    if not isinstance(message, dict):
        raise RuntimeError("summary request first choice has no message object")
    return str(message.get("content") or "").strip()


def compress_session(
    session: Session,
    *,
    convo: List[Dict[str, Any]],
    user_id: int,
    ai_config_id: Optional[int],
    ai_kind: str,
    session_id: str,
    session_name: Optional[str],
    model: Optional[str],
    api_key: str,
    base_url: str,
    system_prompt: str,
    compression_prompt: str,
    session_tokens: int,
    threshold: int,
    keep_recent: int = 4,
) -> Optional[List[Dict[str, Any]]]:
    """Summarize the older part of a session and rebuild the live ``convo``.

    Returns a new ``convo`` list on success, or ``None`` when compression is not
    worth doing or fails (so the caller can avoid retry-looping forever).
    """
    from api.services.model_presets import is_cli_base_url

    if is_cli_base_url(base_url):
        # CLI-backed presets have no HTTP endpoint to run the summary request
        # against; skip quietly so callers fall back to the uncompressed convo.
        return None

    # Load persisted user/assistant messages for this session, excluding ones
    # already folded into a previous summary. Mirrors the runtime history filter.
    # ``phase_summary`` system rows are included too: they are the per-phase
    # progress anchors, so the summary must carry which phases already finished
    # (otherwise a compressed run loses the plan thread and re-plans from phase 1).
    stmt = select(ChatMessage).where(
        ChatMessage.user_id == user_id,
        ChatMessage.session_id == session_id,
        ChatMessage.ai_kind == ai_kind,
        ChatMessage.role.in_(("user", "assistant", "system")),
    ).order_by(ChatMessage.created_at.asc())
    if ai_config_id is not None:
        stmt = stmt.where(ChatMessage.ai_config_id == ai_config_id)

    def _included(m: ChatMessage) -> bool:
        tags = str(getattr(m, "tags", "") or "")
        if "compressed_away" in tags:
            return False
        if m.role == "system":
            # Only the compact phase-progress anchors; other system bubbles
            # (e.g. mcp_tool_call) stay out of the summarization input.
            return "phase_summary" in tags
        return True

    rows: List[ChatMessage] = [m for m in session.exec(stmt).all() if _included(m)]

    if len(rows) < keep_recent + 2:
        # Not enough history to be worth compressing.
        return None

    to_summarize = rows[:-keep_recent] if keep_recent > 0 else rows
    kept = rows[-keep_recent:] if keep_recent > 0 else []
    if not to_summarize:
        return None

    # Build the history text from the to-summarize set.
    history_lines: List[str] = []
    for m in to_summarize:
        label = _ROLE_LABELS.get(str(m.role or ""), str(m.role or ""))
        body = str(m.content or "")
        if len(body) > _MAX_MSG_CHARS:
            body = body[:_MAX_MSG_CHARS] + " …(已截断)"
        history_lines.append(f"{label}: {body}")
    history_text = "\n".join(history_lines)

    template = str(compression_prompt or "").strip() or DEFAULT_COMPRESSION_PROMPT
    if "{history}" in template:
        prompt = template.replace("{history}", history_text)
    else:
        prompt = f"{template}\n\n[待压缩的对话历史]\n{history_text}"

    # Single non-streaming chat completion.
    try:
        resp = ai_http_post(
            base_url,
            headers={
                "Content-Type": "application/json",
                "Authorization": f"Bearer {api_key}",
            },
            json={
                "model": model,
                "messages": [{"role": "user", "content": prompt}],
                "stream": False,
            },
            timeout=120,
        )
        summary = _extract_summary_response(resp)
    except RuntimeError as exc:
        logger.warning("conversation_compress: summary request failed: %s", exc)
        return None
    except Exception as exc:
        logger.exception("conversation_compress: summary request failed unexpectedly: %s", exc)
        return None

    if not summary:
        logger.warning("conversation_compress: summary request returned empty content")
        return None

    summary_content = "[对话历史摘要]\n" + summary

    # Persist: fold the to-summarize rows into the summary.
    try:
        for m in to_summarize:
            existing_tags = [t for t in str(getattr(m, "tags", "") or "").split(",") if t.strip()]
            if "compressed_away" not in existing_tags:
                existing_tags.append("compressed_away")
            m.tags = ",".join(existing_tags)
            m.total_tokens = 0
            session.add(m)
        _save_message(
            session,
            user_id,
            ChatMessageCreate(
                role="user",
                content=summary_content,
                tags="conversation_summary",
                ai_config_id=ai_config_id,
                ai_kind=ai_kind,
                session_id=session_id,
                session_name=session_name,
                model=model,
                total_tokens=max(1, len(summary) // 3),
            ),
        )
        session.commit()
    except Exception:
        logger.exception("conversation_compress: persistence failed")
        session.rollback()
        return None

    # Rebuild the live convo: system + summary + kept-verbatim recent messages.
    new_convo: List[Dict[str, Any]] = [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": summary_content},
    ]
    for m in kept:
        item: Dict[str, Any] = {"role": m.role, "content": m.content}
        if m.role == "assistant" and getattr(m, "think", None):
            item["reasoning_content"] = m.think
        new_convo.append(item)
    return new_convo
