"""Chat message image media helpers: decode/validate base64 image data URLs and
store, fetch, and delete ``ChatMessageMedia`` rows attached to chat messages."""

import base64
import binascii
import secrets
from typing import Iterable

from fastapi import HTTPException
from sqlmodel import Session, select

from api.models import ChatMessage, ChatMessageMedia
from api.services.temp_image_store import DATA_URL_RE, EXT_TO_MIME, _normalize_ext, _validate_image_bytes


def decode_image_data_url(data_url: str) -> tuple[bytes, str]:
    match = DATA_URL_RE.match(str(data_url or ""))
    if not match:
        raise HTTPException(status_code=400, detail="expected data:image/png|jpg|webp;base64,...")
    ext = _normalize_ext(match.group("ext"))
    try:
        raw = base64.b64decode(match.group("data"), validate=False)
    except (binascii.Error, ValueError):
        raise HTTPException(status_code=400, detail="invalid base64 image payload")
    _validate_image_bytes(raw, ext)
    return raw, EXT_TO_MIME[ext]


def save_message_image_data_url(
    session: Session,
    *,
    message: ChatMessage,
    data_url: str,
) -> ChatMessageMedia:
    raw, media_type = decode_image_data_url(data_url)
    row = ChatMessageMedia(
        message_id=int(message.id or 0),
        user_id=int(message.user_id),
        media_type=media_type,
        token=secrets.token_urlsafe(24),
        data=raw,
        bytes=len(raw),
    )
    session.add(row)
    session.commit()
    session.refresh(row)
    return row


def message_media_url(media: ChatMessageMedia) -> str:
    return f"/api/chat/media/{int(media.id or 0)}/{media.token}"


def get_message_media(session: Session, media_id: int, token: str) -> ChatMessageMedia:
    row = session.get(ChatMessageMedia, media_id)
    if not row or not secrets.compare_digest(str(row.token or ""), str(token or "")):
        raise HTTPException(status_code=404, detail="media not found")
    return row


def delete_message_media(session: Session, messages: Iterable[ChatMessage]) -> int:
    ids = [int(msg.id) for msg in messages if msg.id is not None]
    if not ids:
        return 0
    rows = session.exec(select(ChatMessageMedia).where(ChatMessageMedia.message_id.in_(ids))).all()
    for row in rows:
        session.delete(row)
    return len(rows)
