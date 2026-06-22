"""Temp-image routes: create from a data URL or upload (``/api/temp-images``,
``/api/temp-images/upload``) and serve stored images (``/tmp-images/{filename}``),
backed by ``api.services.temp_image_store``."""

from typing import Optional

from fastapi import APIRouter, Depends, File, Header, Request, UploadFile
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field
from sqlmodel import Session

from api.database import get_session
from api.services.temp_image_store import (
    cleanup_expired_temp_images,
    resolve_temp_image,
    save_temp_image,
    save_temp_image_data_url,
)
from api.core.settings import settings
from gateway.routers.auth import get_current_user


router = APIRouter()
PREFIX = ""


class TempImageCreate(BaseModel):
    data_url: str = Field(..., description="data:image/png|jpg|webp;base64,...")


def _public_base_url(request: Request) -> str:
    configured = settings.public_base_url.rstrip("/")
    if configured:
        return configured
    return str(request.base_url).rstrip("/")


def _response_payload(request: Request, filename: str, media_type: str, size: int) -> dict:
    url = f"{_public_base_url(request)}/tmp-images/{filename}"
    return {
        "url": url,
        "image_url": url,
        "file_name": filename,
        "media_type": media_type,
        "bytes": size,
        "ttl_seconds": int(settings.temp_image_ttl_seconds),
    }


@router.post("/api/temp-images")
async def create_temp_image(
    payload: TempImageCreate,
    request: Request,
    authorization: Optional[str] = Header(None),
    session: Session = Depends(get_session),
):
    get_current_user(authorization, session)
    filename, media_type, size = save_temp_image_data_url(payload.data_url)
    cleanup_expired_temp_images()
    return _response_payload(request, filename, media_type, size)


@router.post("/api/temp-images/upload")
async def upload_temp_image(
    request: Request,
    file: UploadFile = File(...),
    authorization: Optional[str] = Header(None),
    session: Session = Depends(get_session),
):
    get_current_user(authorization, session)
    content_type = (file.content_type or "").split(";", 1)[0].lower()
    ext_by_mime = {
        "image/png": "png",
        "image/jpeg": "jpg",
        "image/jpg": "jpg",
        "image/webp": "webp",
    }
    ext = ext_by_mime.get(content_type)
    if not ext and file.filename:
        ext = file.filename.rsplit(".", 1)[-1]
    raw = await file.read()
    filename, media_type, size = save_temp_image(raw, ext or "")
    cleanup_expired_temp_images()
    return _response_payload(request, filename, media_type, size)


@router.get("/tmp-images/{filename}")
async def get_temp_image(filename: str):
    path, media_type = resolve_temp_image(filename)
    return FileResponse(str(path), media_type=media_type)
