"""Public Device Hall catalog, downloads, and client update discovery."""

from __future__ import annotations

from pathlib import Path

import logging
from typing import Optional

from fastapi import APIRouter, Depends, File, Form, HTTPException, Query, Request, UploadFile
from fastapi.responses import FileResponse

from api.services.device_releases import (
    DeviceReleaseError,
    public_catalog,
    resolve_artifact,
    update_info,
)
from api.services.device_release_admin import admin_catalog, publish_release, withdraw_release
from api.models import User
from api.database import get_session
from api.runtime.internal_http import internal_post
from api.core.settings import settings
from api.sio import sio
from gateway.routers.admin import _record_audit, require_admin_user
from sqlmodel import Session
from starlette.concurrency import run_in_threadpool


router = APIRouter()
PREFIX = "/api/device-hall"
logger = logging.getLogger(__name__)


def _base_url(request: Request) -> str:
    return str(request.base_url).rstrip("/")


@router.get("/catalog")
def get_device_catalog(request: Request):
    try:
        return public_catalog(_base_url(request))
    except DeviceReleaseError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc


@router.get("/updates/{product_id}/{target_id}")
def check_device_update(
    product_id: str,
    target_id: str,
    request: Request,
    current_version: str = Query(default="0.0.0", max_length=64),
):
    try:
        return update_info(_base_url(request), product_id, target_id, current_version)
    except DeviceReleaseError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@router.get("/download/{product_id}/{target_id}")
def download_device_release(product_id: str, target_id: str):
    try:
        artifact = resolve_artifact(product_id, target_id)
    except DeviceReleaseError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    return FileResponse(
        path=artifact,
        filename=Path(artifact).name,
        media_type="application/octet-stream",
    )


@router.get("/admin/catalog")
def get_admin_catalog(_admin: User = Depends(require_admin_user)):
    try:
        return admin_catalog()
    except DeviceReleaseError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc


async def _notify_release(payload: dict) -> None:
    """Best-effort notification for monolith and split Connector deployments."""
    try:
        await sio.emit("device:update-available", payload)
    except Exception:
        logger.exception("local device update broadcast failed")
    if settings.connector_runtime_url:
        try:
            await internal_post(
                settings.connector_runtime_url,
                "/internal/device-updates/broadcast",
                json=payload,
                timeout=10,
            )
        except Exception:
            logger.exception("connector device update broadcast failed")


@router.post("/admin/releases")
async def upload_device_release(
    product_id: str = Form(...),
    target_id: str = Form(...),
    version: str = Form(...),
    release_notes: str = Form(default=""),
    mandatory: bool = Form(default=False),
    file: UploadFile = File(...),
    actor: User = Depends(require_admin_user),
    session: Session = Depends(get_session),
):
    try:
        result = await run_in_threadpool(
            publish_release,
            product_id=product_id,
            target_id=target_id,
            version=version,
            filename=file.filename or "",
            stream=file.file,
            release_notes=release_notes,
            mandatory=mandatory,
            max_bytes=settings.device_release_max_bytes,
        )
    except DeviceReleaseError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    finally:
        await file.close()
    _record_audit(
        session, actor, "device_release.publish",
        target_type="device_release", target_id=f"{product_id}/{target_id}",
        target_label=version,
    )
    await _notify_release({
        "product_id": product_id,
        "target_id": target_id,
        "latest_version": version,
        "mandatory": mandatory,
        "release_notes": release_notes,
    })
    return result


@router.delete("/admin/releases/{product_id}/{target_id}")
def delete_device_release(
    product_id: str,
    target_id: str,
    version: Optional[str] = Query(default=None, max_length=64),
    delete_artifact: bool = Query(default=False),
    actor: User = Depends(require_admin_user),
    session: Session = Depends(get_session),
):
    try:
        result = withdraw_release(
            product_id, target_id, version=version,
            delete_artifact=delete_artifact,
        )
    except DeviceReleaseError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    _record_audit(
        session, actor, "device_release.withdraw",
        target_type="device_release", target_id=f"{product_id}/{target_id}",
        target_label=version or "current",
    )
    return result
