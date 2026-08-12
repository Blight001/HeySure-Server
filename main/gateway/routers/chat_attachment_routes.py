"""Upload and serve workspace-backed chat attachments."""

IS_ROUTER_ENTRY = False

from pathlib import Path
from typing import Optional

from fastapi import Depends, File, Form, Header, HTTPException, UploadFile
from fastapi.responses import FileResponse
from fastapi.concurrency import run_in_threadpool
from sqlmodel import Session, select

from api.database import get_session
from api.models import AssistantAIConfig
from api.services.chat.chat_attachments import get_message_attachment
from api.services.storage.workspace_files import (
    save_workspace_stream,
)
from .auth import get_current_user
from .chat_base import router


def _validate_attachment_target(
    session: Session,
    user_id: int,
    ai_config_id: Optional[int],
) -> None:
    if ai_config_id is None:
        return
    exists = session.exec(
        select(AssistantAIConfig.id).where(
            AssistantAIConfig.id == ai_config_id,
            AssistantAIConfig.user_id == user_id,
        )
    ).first()
    if exists is None:
        raise HTTPException(status_code=404, detail="AI member not found")


@router.post("/attachments/upload")
async def upload_chat_attachment(
    file: UploadFile = File(...),
    ai_config_id: Optional[int] = Form(None),
    ai_kind: str = Form("assistant"),
    session_id: str = Form("default"),
    session: Session = Depends(get_session),
    authorization: str = Header(None),
):
    user = get_current_user(authorization, session)
    if ai_kind not in {"assistant", "core"}:
        raise HTTPException(status_code=400, detail="ai_kind is invalid")
    _validate_attachment_target(session, user.id, ai_config_id)
    filename = Path(str(file.filename or "file.bin")).name or "file.bin"
    record = await run_in_threadpool(
        save_workspace_stream,
        user_id=user.id,
        ai_config_id=ai_config_id,
        source=file.file,
        file_name=filename,
        folder="Uploads",
    )
    record["is_image"] = str(record.get("mime_type") or "").startswith("image/")
    record["session_id"] = str(session_id or "default")
    record.pop("server_path", None)
    return record


@router.get("/attachments/{attachment_id}/{token}")
def serve_chat_attachment(
    attachment_id: int,
    token: str,
    session: Session = Depends(get_session),
):
    row, record = get_message_attachment(session, attachment_id, token)
    return FileResponse(
        record["server_path"],
        media_type=row.mime_type or "application/octet-stream",
        filename=row.file_name,
        content_disposition_type="inline" if row.mime_type.startswith("image/") else "attachment",
        headers={"Cache-Control": "private, max-age=31536000, immutable"},
    )
