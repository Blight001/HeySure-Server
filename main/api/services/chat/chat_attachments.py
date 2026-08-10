"""Workspace-backed attachments associated with persisted chat messages."""

from __future__ import annotations

import secrets
import base64
from collections import defaultdict
from typing import Any, Iterable, Optional

from fastapi import HTTPException
from sqlmodel import Session, select

from api.models import ChatMessageAttachment
from api.services.storage.workspace_files import resolve_file_ref


MAX_CHAT_ATTACHMENTS = 5
MAX_MODEL_IMAGE_BYTES = 10 * 1024 * 1024
MODEL_IMAGE_TYPES = {"image/jpeg", "image/png", "image/webp", "image/gif"}


def normalize_file_refs(raw: Any) -> list[str]:
    if raw in (None, ""):
        return []
    if not isinstance(raw, list):
        raise HTTPException(status_code=400, detail="attachments must be a list")
    if len(raw) > MAX_CHAT_ATTACHMENTS:
        raise HTTPException(
            status_code=400,
            detail=f"每条消息最多上传 {MAX_CHAT_ATTACHMENTS} 个附件",
        )
    refs: list[str] = []
    for item in raw:
        value = item.get("file_ref") if isinstance(item, dict) else item
        file_ref = str(value or "").strip()
        if file_ref and file_ref not in refs:
            refs.append(file_ref)
    if len(refs) > MAX_CHAT_ATTACHMENTS:
        raise HTTPException(
            status_code=400,
            detail=f"每条消息最多上传 {MAX_CHAT_ATTACHMENTS} 个附件",
        )
    return refs


def resolve_attachment_refs(
    *,
    user_id: int,
    ai_config_id: Optional[int],
    raw: Any,
) -> list[dict[str, Any]]:
    return [
        resolve_file_ref(
            user_id=user_id,
            ai_config_id=ai_config_id,
            file_ref=file_ref,
        )
        for file_ref in normalize_file_refs(raw)
    ]


def model_attachment_section(records: Iterable[dict[str, Any]]) -> str:
    files = list(records)
    if not files:
        return ""
    lines = [
        "[本轮上传附件]",
        "文件已保存到当前 AI 的工作目录。可按下列相对路径使用工作区工具读取；不要猜测文件内容。",
    ]
    for item in files:
        kind = "图片" if str(item.get("mime_type") or "").startswith("image/") else "文件"
        lines.append(
            f'- {kind}「{item.get("file_name") or "未命名文件"}」：'
            f'{item.get("workspace_path") or ""}（file_ref: {item.get("file_ref") or ""}）'
        )
    if any(str(item.get("mime_type") or "").startswith("image/") for item in files):
        lines.append(
            "兼容的图片会同时作为视觉输入提供；若当前模型不支持图片，系统会自动移除视觉块，"
            "你应明确说明无法直接看图，并改用工作区路径和可用工具处理。"
        )
    return "\n".join(lines)


def message_model_content(
    session: Session,
    *,
    message_id: int,
    user_id: int,
    ai_config_id: Optional[int],
    text: str,
    include_manifest: bool = True,
) -> str | list[dict[str, Any]]:
    rows = session.exec(
        select(ChatMessageAttachment).where(
            ChatMessageAttachment.message_id == int(message_id),
            ChatMessageAttachment.user_id == int(user_id),
        ).order_by(ChatMessageAttachment.id.asc())
    ).all()
    records = [{
        "file_ref": row.file_ref,
        "workspace_path": row.workspace_path,
        "file_name": row.file_name,
        "mime_type": row.mime_type,
        "bytes": row.bytes,
    } for row in rows]
    model_text = str(text or "")
    if include_manifest:
        section = model_attachment_section(records)
        model_text = "\n\n".join(part for part in (model_text, section) if part)
    blocks = _message_image_blocks(rows, user_id, ai_config_id)
    if not blocks:
        return model_text
    return [{"type": "text", "text": model_text}, *blocks]


def _message_image_blocks(rows, user_id: int, ai_config_id: Optional[int]):
    blocks = []
    for row in rows:
        mime_type = str(row.mime_type or "").lower()
        if mime_type not in MODEL_IMAGE_TYPES or int(row.bytes or 0) > MAX_MODEL_IMAGE_BYTES:
            continue
        try:
            record = resolve_file_ref(
                user_id=user_id,
                ai_config_id=ai_config_id,
                file_ref=row.file_ref,
            )
            with open(record["server_path"], "rb") as handle:
                encoded = base64.b64encode(handle.read()).decode("ascii")
        except (OSError, HTTPException):
            continue
        blocks.append({
            "type": "image_url",
            "image_url": {"url": f"data:{mime_type};base64,{encoded}"},
        })
    return blocks


def bind_message_attachments(
    session: Session,
    *,
    message_id: int,
    user_id: int,
    ai_config_id: Optional[int],
    records: Iterable[dict[str, Any]],
) -> list[ChatMessageAttachment]:
    rows: list[ChatMessageAttachment] = []
    for item in records:
        row = ChatMessageAttachment(
            message_id=message_id,
            user_id=user_id,
            ai_config_id=ai_config_id,
            file_ref=str(item["file_ref"]),
            workspace_path=str(item["workspace_path"]),
            file_name=str(item["file_name"]),
            mime_type=str(item.get("mime_type") or "application/octet-stream"),
            bytes=int(item.get("bytes") or 0),
            token=secrets.token_urlsafe(24),
        )
        session.add(row)
        rows.append(row)
    if rows:
        session.commit()
        for row in rows:
            session.refresh(row)
    return rows


def attachment_url(row: ChatMessageAttachment) -> str:
    return f"/api/chat/attachments/{row.id}/{row.token}"


def attachment_payload(row: ChatMessageAttachment) -> dict[str, Any]:
    return {
        "id": row.id,
        "file_ref": row.file_ref,
        "workspace_path": row.workspace_path,
        "file_name": row.file_name,
        "mime_type": row.mime_type,
        "bytes": row.bytes,
        "is_image": str(row.mime_type or "").startswith("image/"),
        "url": attachment_url(row),
    }


def message_attachment_map(
    session: Session,
    message_ids: Iterable[int],
) -> dict[int, list[dict[str, Any]]]:
    ids = [int(value) for value in message_ids if value is not None]
    if not ids:
        return {}
    rows = session.exec(
        select(ChatMessageAttachment)
        .where(ChatMessageAttachment.message_id.in_(ids))
        .order_by(ChatMessageAttachment.id.asc())
    ).all()
    grouped: dict[int, list[dict[str, Any]]] = defaultdict(list)
    for row in rows:
        grouped[row.message_id].append(attachment_payload(row))
    return dict(grouped)


def get_message_attachment(
    session: Session,
    attachment_id: int,
    token: str,
) -> tuple[ChatMessageAttachment, dict[str, Any]]:
    row = session.get(ChatMessageAttachment, attachment_id)
    if not row or not secrets.compare_digest(str(row.token or ""), str(token or "")):
        raise HTTPException(status_code=404, detail="Attachment not found")
    record = resolve_file_ref(
        user_id=row.user_id,
        ai_config_id=row.ai_config_id,
        file_ref=row.file_ref,
    )
    return row, record
