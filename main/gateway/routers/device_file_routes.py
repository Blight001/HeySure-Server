"""Temporary workspace-file links and authenticated device link issuance."""

from __future__ import annotations

from urllib.parse import quote

from fastapi import APIRouter, Depends, Header, Request
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field
from sqlmodel import Session

from api.core.settings import settings
from api.database import get_session
from api.services.storage.temporary_file_links import (
    DEFAULT_TTL_SECONDS,
    create_temporary_file_link,
    resolve_temporary_file_link,
)
from .auth import get_current_user


router = APIRouter()
PREFIX = "/api"


class DeviceFileLinkRequest(BaseModel):
    ai_config_id: int = Field(gt=0)
    file_refs: list[str] = Field(min_length=1, max_length=5)
    ttl_seconds: int = Field(default=DEFAULT_TTL_SECONDS, ge=60, le=900)


def _public_base(request: Request) -> str:
    configured = str(settings.public_base_url or "").strip().rstrip("/")
    if configured:
        return configured
    forwarded_host = str(request.headers.get("x-forwarded-host") or "").split(",", 1)[0].strip()
    forwarded_proto = str(request.headers.get("x-forwarded-proto") or "").split(",", 1)[0].strip().lower()
    if forwarded_host and not any(char.isspace() or char in "/\\" for char in forwarded_host):
        scheme = forwarded_proto if forwarded_proto in {"http", "https"} else request.url.scheme
        return f"{scheme}://{forwarded_host}"
    return str(request.base_url).rstrip("/")


@router.post("/devices/files/links")
def create_device_file_links(
    payload: DeviceFileLinkRequest,
    request: Request,
    session: Session = Depends(get_session),
    authorization: str = Header(None),
):
    """Create temporary public links after authenticating the endpoint user."""
    user = get_current_user(authorization, session)
    refs = [str(item or "").strip() for item in payload.file_refs]
    if len(set(refs)) != len(refs):
        from fastapi import HTTPException

        raise HTTPException(status_code=400, detail="file_refs must be unique")
    links = [
        create_temporary_file_link(
            user_id=user.id,
            ai_config_id=payload.ai_config_id,
            file_ref=file_ref,
            ttl_seconds=payload.ttl_seconds,
            public_base_url=_public_base(request),
        )
        for file_ref in refs
    ]
    return {"links": links, "count": len(links)}


@router.get("/tmp-files/{grant_id}/{token}")
def download_temporary_file(grant_id: str, token: str):
    """Serve a file to anyone possessing an unexpired opaque capability URL."""
    record = resolve_temporary_file_link(grant_id, token)
    return FileResponse(
        record["server_path"],
        media_type=record["mime_type"] or "application/octet-stream",
        filename=record["file_name"],
        content_disposition_type="attachment",
        headers={
            "Cache-Control": "private, no-store",
            "X-Content-Type-Options": "nosniff",
            "X-HeySure-File-Name": quote(str(record["file_name"]), safe=""),
            "X-HeySure-File-Ref": record["file_ref"],
            "X-HeySure-File-Sha256": record["sha256"],
            "X-HeySure-Link-Expires-At": str(record["expires_at"]),
        },
    )
