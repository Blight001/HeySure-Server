"""Public Device Hall catalog, downloads, and client update discovery."""

from __future__ import annotations

from pathlib import Path

from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.responses import FileResponse

from api.services.device_releases import (
    DeviceReleaseError,
    public_catalog,
    resolve_artifact,
    update_info,
)
router = APIRouter()
PREFIX = "/api/device-hall"


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
