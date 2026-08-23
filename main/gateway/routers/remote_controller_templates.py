"""Authenticated low-frequency CRUD for remote-controller templates."""

from typing import Optional

from fastapi import APIRouter, Depends, Header, HTTPException, Query, Response
from sqlmodel import Session

from api.database import get_session
from api.services.remote_control.controller_schema import (
    Capability,
    DeviceType,
    RestoreRequest,
    SCHEMA_NAME,
    TemplateCreate,
    TemplateId,
    TemplateUpdate,
    contract_json_schemas,
)
from api.services.remote_control.controller_templates import (
    BUILTIN_REVISION,
    TemplateConflictError,
    TemplateLimitError,
    TemplateNotFoundError,
    create_template,
    delete_template,
    get_template,
    list_templates,
    restore_builtin,
    template_etag,
    update_template,
)
from gateway.routers.auth import get_current_user


router = APIRouter()
PREFIX = "/api/remote-controller-templates"


def _payload(document) -> dict:
    return document.model_dump(mode="json", by_alias=True, exclude_none=True)


def _set_document_headers(response: Response, document) -> None:
    response.headers["ETag"] = template_etag(document)
    response.headers["Cache-Control"] = "private, no-store, max-age=0"


def _raise_service_error(exc: Exception) -> None:
    if isinstance(exc, TemplateNotFoundError):
        raise HTTPException(status_code=404, detail={"code": "TEMPLATE_NOT_FOUND"}) from exc
    if isinstance(exc, TemplateLimitError):
        raise HTTPException(status_code=409, detail={"code": "TEMPLATE_LIMIT_REACHED"}) from exc
    if isinstance(exc, TemplateConflictError):
        raise HTTPException(status_code=409, detail={"code": "TEMPLATE_REVISION_CONFLICT"}) from exc
    raise exc


@router.get("/schema")
def get_template_schema(
    session: Session = Depends(get_session),
    authorization: Optional[str] = Header(None),
):
    get_current_user(authorization, session)
    return {"schema": SCHEMA_NAME, "schemas": contract_json_schemas()}


@router.get("")
def list_remote_controller_templates(
    response: Response,
    device_type: Optional[DeviceType] = Query(default=None, alias="deviceType"),
    capability: Optional[Capability] = Query(default=None),
    session: Session = Depends(get_session),
    authorization: Optional[str] = Header(None),
):
    user = get_current_user(authorization, session)
    items = list_templates(
        session,
        user.id,
        device_type=device_type,
        capability=capability,
    )
    response.headers["Cache-Control"] = "private, no-store, max-age=0"
    return {
        "schema": SCHEMA_NAME,
        "items": [_payload(item) for item in items],
        "total": len(items),
        "defaultsRevision": BUILTIN_REVISION,
    }


@router.post("", status_code=201)
def create_remote_controller_template(
    body: TemplateCreate,
    response: Response,
    session: Session = Depends(get_session),
    authorization: Optional[str] = Header(None),
):
    user = get_current_user(authorization, session)
    try:
        document = create_template(session, user.id, body)
    except (TemplateNotFoundError, TemplateConflictError, TemplateLimitError) as exc:
        _raise_service_error(exc)
    _set_document_headers(response, document)
    return _payload(document)


@router.get("/{template_id}")
def get_remote_controller_template(
    template_id: TemplateId,
    response: Response,
    session: Session = Depends(get_session),
    authorization: Optional[str] = Header(None),
):
    user = get_current_user(authorization, session)
    try:
        document = get_template(session, user.id, template_id)
    except TemplateNotFoundError as exc:
        _raise_service_error(exc)
    _set_document_headers(response, document)
    return _payload(document)


@router.put("/{template_id}")
def update_remote_controller_template(
    template_id: TemplateId,
    body: TemplateUpdate,
    response: Response,
    session: Session = Depends(get_session),
    authorization: Optional[str] = Header(None),
):
    user = get_current_user(authorization, session)
    try:
        document = update_template(session, user.id, template_id, body)
    except (TemplateNotFoundError, TemplateConflictError) as exc:
        _raise_service_error(exc)
    _set_document_headers(response, document)
    return _payload(document)


@router.delete("/{template_id}")
def delete_remote_controller_template(
    template_id: TemplateId,
    expected_revision: int = Query(alias="expectedRevision", ge=1),
    session: Session = Depends(get_session),
    authorization: Optional[str] = Header(None),
):
    user = get_current_user(authorization, session)
    try:
        revision = delete_template(session, user.id, template_id, expected_revision)
    except (TemplateNotFoundError, TemplateConflictError) as exc:
        _raise_service_error(exc)
    return {"deleted": True, "id": template_id, "revision": revision}


@router.post("/{template_id}/restore")
def restore_remote_controller_template(
    template_id: TemplateId,
    body: RestoreRequest,
    response: Response,
    session: Session = Depends(get_session),
    authorization: Optional[str] = Header(None),
):
    user = get_current_user(authorization, session)
    try:
        document = restore_builtin(session, user.id, template_id, body.expected_revision)
    except (TemplateNotFoundError, TemplateConflictError) as exc:
        _raise_service_error(exc)
    _set_document_headers(response, document)
    return _payload(document)
