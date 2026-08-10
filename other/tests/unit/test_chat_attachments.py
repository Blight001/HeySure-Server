from pathlib import Path

import pytest
from fastapi import HTTPException
from sqlmodel import Session, create_engine

from api.models import ChatMessage, ChatMessageAttachment
from api.services.chat.chat_attachments import (
    bind_message_attachments,
    message_attachment_map,
    model_attachment_section,
    normalize_file_refs,
)
from ai_runtime.inference.input_attachments import apply_current_message_images


def _engine():
    engine = create_engine("sqlite://")
    ChatMessage.__table__.create(engine)
    ChatMessageAttachment.__table__.create(engine)
    return engine


def test_normalize_file_refs_deduplicates_and_enforces_limit():
    assert normalize_file_refs([
        "file_1",
        {"file_ref": "file_1"},
        {"file_ref": "file_2"},
    ]) == ["file_1", "file_2"]
    with pytest.raises(HTTPException) as exc:
        normalize_file_refs([f"file_{idx}" for idx in range(6)])
    assert exc.value.status_code == 400


def test_bind_and_history_payload_keep_only_workspace_reference():
    engine = _engine()
    with Session(engine) as session:
        message = ChatMessage(user_id=7, role="user", content="附件")
        session.add(message)
        session.commit()
        session.refresh(message)
        rows = bind_message_attachments(
            session,
            message_id=message.id,
            user_id=7,
            ai_config_id=9,
            records=[{
                "file_ref": "file_abc",
                "workspace_path": "Uploads/example.txt",
                "file_name": "example.txt",
                "mime_type": "text/plain",
                "bytes": 12,
            }],
        )
        payload = message_attachment_map(session, [message.id])[message.id][0]

    assert rows[0].file_ref == "file_abc"
    assert payload["workspace_path"] == "Uploads/example.txt"
    assert "server_path" not in payload
    assert payload["url"].startswith("/api/chat/attachments/")


def test_current_message_image_is_added_as_multimodal_block(tmp_path, monkeypatch):
    image = Path(tmp_path, "sample.png")
    image.write_bytes(b"\x89PNG\r\n\x1a\nsmall")
    engine = _engine()
    with Session(engine) as session:
        message = ChatMessage(user_id=3, ai_config_id=4, role="user", content="看图")
        session.add(message)
        session.commit()
        session.refresh(message)
        session.add(ChatMessageAttachment(
            message_id=message.id,
            user_id=3,
            ai_config_id=4,
            file_ref="file_image",
            workspace_path="Uploads/sample.png",
            file_name="sample.png",
            mime_type="image/png",
            bytes=image.stat().st_size,
            token="token",
        ))
        session.commit()
        monkeypatch.setattr(
            "api.services.chat.chat_attachments.resolve_file_ref",
            lambda **_kwargs: {"server_path": str(image)},
        )
        conversation = [
            {"role": "system", "content": "system"},
            {"role": "user", "content": "看图"},
        ]
        count = apply_current_message_images(
            session,
            conversation,
            message_id=message.id,
            user_id=3,
            ai_config_id=4,
        )

    assert count == 1
    assert conversation[-1]["content"][0] == {"type": "text", "text": "看图"}
    assert conversation[-1]["content"][1]["type"] == "image_url"
    assert conversation[-1]["content"][1]["image_url"]["url"].startswith("data:image/png;base64,")


def test_attachment_manifest_explains_workspace_and_vision_fallback():
    section = model_attachment_section([{
        "file_ref": "file_image",
        "workspace_path": "Uploads/photo.png",
        "file_name": "photo.png",
        "mime_type": "image/png",
    }])
    assert "Uploads/photo.png" in section
    assert "视觉输入" in section
    assert "不支持图片" in section
