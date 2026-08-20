"""Privileged Device Hall publishing routes."""

from __future__ import annotations

import logging
from typing import Optional

from fastapi import APIRouter, Depends, File, Form, HTTPException, Query, UploadFile
from sqlmodel import Session
from starlette.concurrency import run_in_threadpool

from api.core.settings import settings
from api.database import get_session
from api.models import User
from api.runtime.internal_http import internal_post
from api.services.device_release_admin import (
    PublishReleaseInput,
    admin_catalog,
    publish_release,
    withdraw_release,
)
from api.services.device_releases import DeviceReleaseError
from api.sio import sio
from gateway.routers.admin import _record_audit, require_admin_user


router = APIRouter()
PREFIX = "/api/device-hall"
logger = logging.getLogger(__name__)


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
    if not settings.connector_runtime_url:
        return
    try:
        await internal_post(
            settings.connector_runtime_url,
            "/internal/device-updates/broadcast",
            json=payload,
            timeout=10,
        )
    except Exception:
        logger.exception("connector device update broadcast failed")


def _publish_input(product_id, target_id, version, filename, release_notes, mandatory):
    return PublishReleaseInput(
        product_id=product_id,
        target_id=target_id,
        version=version,
        filename=filename,
        release_notes=release_notes,
        mandatory=mandatory,
    )


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
    request = _publish_input(
        product_id, target_id, version, file.filename or "", release_notes, mandatory,
    )
    try:
        result = await run_in_threadpool(
            publish_release, file.file, request=request,
            max_bytes=settings.device_release_max_bytes,
        )
    except DeviceReleaseError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    finally:
        await file.close()
    _record_publish_audit(session, actor, request)
    await _notify_release(_notification(request))
    return result


def _record_publish_audit(session, actor, request):
    _record_audit(
        session, actor, "device_release.publish",
        target_type="device_release",
        target_id=f"{request.product_id}/{request.target_id}",
        target_label=request.version,
    )


def _notification(request):
    return {
        "product_id": request.product_id,
        "target_id": request.target_id,
        "latest_version": request.version,
        "mandatory": request.mandatory,
        "release_notes": request.release_notes,
    }


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
