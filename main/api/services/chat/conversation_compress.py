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
from dataclasses import dataclass
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

_COMPRESSION_STARTED_NOTICE = (
    "[系统提示]\n正在自动压缩较早的对话历史；系统会保留最近消息，"
    "并用详细摘要承接目标、约束、进度、关键数据、待办与风险。"
)
_COMPRESSION_FAILED_NOTICE = (
    "[系统提示]\n对话历史自动压缩未完成；系统已保留原始消息，"
    "并会继续当前对话。"
)


@dataclass(frozen=True)
class _CompressionMessageContext:
    user_id: int
    ai_config_id: Optional[int]
    ai_kind: str
    session_id: str
    session_name: Optional[str]
    model: Optional[str]


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


def _persist_compression_notice(
    session: Session,
    *,
    message_context: _CompressionMessageContext,
    content: str,
    tags: str,
) -> bool:
    """Persist a compression status bubble immediately for history polling."""
    try:
        # _save_message commits before it returns, so the active-run history
        # poller can see this bubble while the summary model call is in flight.
        _save_message(
            session,
            message_context.user_id,
            ChatMessageCreate(
                role="system",
                content=content,
                tags=tags,
                ai_config_id=message_context.ai_config_id,
                ai_kind=message_context.ai_kind,
                session_id=message_context.session_id,
                session_name=message_context.session_name,
                model=message_context.model,
                total_tokens=0,
            ),
        )
        return True
    except Exception:
        logger.exception("conversation_compress: status notice persistence failed tags=%s", tags)
        session.rollback()
        return False


def _load_compression_rows(
    session: Session,
    *,
    message_context: _CompressionMessageContext,
) -> List[ChatMessage]:
    stmt = select(ChatMessage).where(
        ChatMessage.user_id == message_context.user_id,
        ChatMessage.session_id == message_context.session_id,
        ChatMessage.ai_kind == message_context.ai_kind,
        ChatMessage.role.in_(("user", "assistant", "system")),
    ).order_by(ChatMessage.created_at.asc())
    if message_context.ai_config_id is not None:
        stmt = stmt.where(ChatMessage.ai_config_id == message_context.ai_config_id)

    def included(message: ChatMessage) -> bool:
        tags = str(getattr(message, "tags", "") or "")
        if "compressed_away" in tags:
            return False
        if message.role == "system":
            return "phase_summary" in tags
        return True

    return [message for message in session.exec(stmt).all() if included(message)]


def _build_compression_prompt(rows: List[ChatMessage], compression_prompt: str) -> str:
    history_lines: List[str] = []
    for message in rows:
        label = _ROLE_LABELS.get(str(message.role or ""), str(message.role or ""))
        body = str(message.content or "")
        if len(body) > _MAX_MSG_CHARS:
            body = body[:_MAX_MSG_CHARS] + " …(已截断)"
        history_lines.append(f"{label}: {body}")
    history_text = "\n".join(history_lines)
    template = str(compression_prompt or "").strip() or DEFAULT_COMPRESSION_PROMPT
    if "{history}" in template:
        return template.replace("{history}", history_text)
    return f"{template}\n\n[待压缩的对话历史]\n{history_text}"


def _request_summary(
    *,
    base_url: str,
    api_key: str,
    model: Optional[str],
    prompt: str,
) -> Optional[str]:
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
    return summary


def _persist_compression_result(
    session: Session,
    *,
    message_context: _CompressionMessageContext,
    to_summarize: List[ChatMessage],
    summary: str,
) -> Optional[str]:
    summary_content = "[系统提示]\n对话压缩摘要\n\n" + summary
    try:
        for message in to_summarize:
            tags = [tag for tag in str(getattr(message, "tags", "") or "").split(",") if tag.strip()]
            if "compressed_away" not in tags:
                tags.append("compressed_away")
            message.tags = ",".join(tags)
            message.total_tokens = 0
            session.add(message)
        _save_message(
            session,
            message_context.user_id,
            ChatMessageCreate(
                role="system",
                content=summary_content,
                tags="conversation_summary,system_notice_compress_result",
                ai_config_id=message_context.ai_config_id,
                ai_kind=message_context.ai_kind,
                session_id=message_context.session_id,
                session_name=message_context.session_name,
                model=message_context.model,
                total_tokens=max(1, len(summary) // 3),
            ),
        )
        session.commit()
        return summary_content
    except Exception:
        logger.exception("conversation_compress: persistence failed")
        session.rollback()
        return None


def _rebuild_conversation(
    *,
    system_prompt: str,
    summary_content: str,
    kept: List[ChatMessage],
) -> List[Dict[str, Any]]:
    new_convo: List[Dict[str, Any]] = [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": summary_content},
    ]
    for message in kept:
        item: Dict[str, Any] = {"role": message.role, "content": message.content}
        if message.role == "assistant" and getattr(message, "think", None):
            item["reasoning_content"] = message.think
        new_convo.append(item)
    return new_convo


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
    message_context = _CompressionMessageContext(
        user_id=user_id,
        ai_config_id=ai_config_id,
        ai_kind=ai_kind,
        session_id=session_id,
        session_name=session_name,
        model=model,
    )
    rows = _load_compression_rows(session, message_context=message_context)
    if len(rows) < keep_recent + 2:
        return None
    to_summarize = rows[:-keep_recent] if keep_recent > 0 else rows
    kept = rows[-keep_recent:] if keep_recent > 0 else []
    if not to_summarize:
        return None
    prompt = _build_compression_prompt(to_summarize, compression_prompt)
    if not _persist_compression_notice(
        session,
        message_context=message_context,
        content=_COMPRESSION_STARTED_NOTICE,
        tags="system_notice_compress_started",
    ):
        return None
    summary = _request_summary(base_url=base_url, api_key=api_key, model=model, prompt=prompt)
    if summary is None:
        _persist_compression_notice(
            session,
            message_context=message_context,
            content=_COMPRESSION_FAILED_NOTICE,
            tags="system_notice_compress_failed",
        )
        return None
    summary_content = _persist_compression_result(
        session,
        message_context=message_context,
        to_summarize=to_summarize,
        summary=summary,
    )
    if summary_content is None:
        return None
    return _rebuild_conversation(
        system_prompt=system_prompt,
        summary_content=summary_content,
        kept=kept,
    )
