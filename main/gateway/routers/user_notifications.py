"""Authenticated HeySure inbox and notification attachment routes."""

from fastapi import APIRouter, Depends, Header, HTTPException, Query
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field
from sqlmodel import Session

from api.database import get_session
from api.models.user_notification import UserNotification
from api.services.notifications.user_notifications import (
    list_notifications,
    mark_all_read,
    mark_read,
    notification_payload,
)
from api.services.notifications.push_delivery import (
    disable_endpoint,
    endpoint_metadata,
    upsert_endpoint,
)
from api.services.storage.workspace_files import resolve_file_ref

from .auth import get_current_user


router = APIRouter()
PREFIX = "/api"


class PushEndpointRegistration(BaseModel):
    provider: str = Field(default="huawei", max_length=32)
    push_token: str = Field(min_length=1, max_length=4096)
    app_version: str = Field(default="", max_length=40)


def _current(authorization: str, session: Session):
    return get_current_user(authorization, session)


@router.get("/user-notifications")
def notification_list(
    unread_only: bool = Query(default=False),
    limit: int = Query(default=100, ge=1, le=200),
    session: Session = Depends(get_session),
    authorization: str = Header(None),
):
    user = _current(authorization, session)
    return {"items": list_notifications(session, user_id=user.id, unread_only=unread_only, limit=limit)}


@router.post("/user-notifications/read-all")
def notification_read_all(
    session: Session = Depends(get_session),
    authorization: str = Header(None),
):
    user = _current(authorization, session)
    return {"updated": mark_all_read(session, user_id=user.id)}


@router.put("/user-notifications/push-endpoints/{device_id}")
def push_endpoint_register(
    device_id: str,
    body: PushEndpointRegistration,
    session: Session = Depends(get_session),
    authorization: str = Header(None),
):
    user = _current(authorization, session)
    try:
        item = upsert_endpoint(
            session,
            user_id=user.id,
            provider=body.provider,
            device_id=device_id,
            push_token=body.push_token,
            app_version=body.app_version,
        )
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    return endpoint_metadata(item)


@router.delete("/user-notifications/push-endpoints/{device_id}")
def push_endpoint_unregister(
    device_id: str,
    provider: str = Query(default="huawei", max_length=32),
    session: Session = Depends(get_session),
    authorization: str = Header(None),
):
    user = _current(authorization, session)
    item = disable_endpoint(
        session,
        user_id=user.id,
        provider=provider,
        device_id=device_id,
    )
    return {"disabled": bool(item)}


@router.post("/user-notifications/{notification_id}/read")
def notification_read(
    notification_id: str,
    session: Session = Depends(get_session),
    authorization: str = Header(None),
):
    user = _current(authorization, session)
    item = mark_read(session, user_id=user.id, notification_id=notification_id)
    if not item:
        raise HTTPException(status_code=404, detail="notification not found")
    return notification_payload(item)


@router.get("/user-notifications/{notification_id}/attachments/{attachment_index}")
def notification_attachment(
    notification_id: str,
    attachment_index: int,
    session: Session = Depends(get_session),
    authorization: str = Header(None),
):
    user = _current(authorization, session)
    item = session.get(UserNotification, notification_id)
    if not item or item.user_id != user.id:
        raise HTTPException(status_code=404, detail="notification not found")
    payload = notification_payload(item)
    attachments = payload.get("attachments") or []
    if attachment_index < 0 or attachment_index >= len(attachments):
        raise HTTPException(status_code=404, detail="attachment not found")
    attachment = attachments[attachment_index]
    file_ref = str(attachment.get("file_ref") or "")
    if not file_ref:
        raise HTTPException(status_code=404, detail="attachment is not stored in HeySure")
    record = resolve_file_ref(
        user_id=user.id,
        ai_config_id=item.ai_config_id,
        file_ref=file_ref,
    )
    return FileResponse(
        record["server_path"],
        media_type=record["mime_type"],
        filename=record["file_name"],
    )
