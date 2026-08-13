"""Persist decrypted WeChat media as scoped chat attachments."""

from __future__ import annotations

from typing import Any, Dict, Iterable, Optional

from sqlmodel import Session

from api.services.chat.chat_attachments import bind_message_attachments, message_model_content
from api.services.storage.workspace_files import save_workspace_bytes
from .media import download_items


def bind_wechat_media(
    session: Session,
    *,
    message_id: int,
    user_id: int,
    ai_config_id: int,
    text: str,
    items: Iterable[Dict[str, Any]],
) -> str | list[dict[str, Any]]:
    media_items = [item for item in items if int(item.get("type") or 0) in {2, 3, 4, 5}]
    records = []
    for item in download_items(media_items):
        record = save_workspace_bytes(
            user_id=user_id,
            ai_config_id=ai_config_id,
            data=item["data"],
            file_name=item["file_name"],
            folder="WeChat",
        )
        records.append(record)
    if records:
        bind_message_attachments(
            session,
            message_id=message_id,
            user_id=user_id,
            ai_config_id=ai_config_id,
            records=records,
        )
    elif media_items:
        text = f"{text}\n\n[系统提示：微信媒体下载或解密失败，本轮没有可读取的附件。]"
    return message_model_content(
        session,
        message_id=message_id,
        user_id=user_id,
        ai_config_id=ai_config_id,
        text=text,
    )
