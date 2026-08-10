"""Attach compatible workspace images to the current model user turn."""

from __future__ import annotations

from typing import Any, Optional

from sqlmodel import Session

from api.services.chat.chat_attachments import message_model_content


def apply_current_message_images(
    session: Session,
    conversation: list[dict[str, Any]],
    *,
    message_id: Optional[int],
    user_id: int,
    ai_config_id: Optional[int],
) -> int:
    """Append safe data-url image blocks to the latest user message.

    Unsupported image formats and oversized images remain available by their
    workspace path in the textual attachment manifest; they are simply not
    embedded in the provider request.
    """

    if message_id is None:
        return 0
    content = message_model_content(
        session,
        message_id=message_id,
        user_id=user_id,
        ai_config_id=ai_config_id,
        text="",
        include_manifest=False,
    )
    if not isinstance(content, list):
        return 0
    blocks = [item for item in content if item.get("type") == "image_url"]
    if not blocks:
        return 0
    for index in range(len(conversation) - 1, -1, -1):
        if conversation[index].get("role") != "user":
            continue
        current = conversation[index].get("content")
        text_blocks = current if isinstance(current, list) else [
            {"type": "text", "text": str(current or "")}
        ]
        conversation[index] = {**conversation[index], "content": [*text_blocks, *blocks]}
        return len(blocks)
    return 0
