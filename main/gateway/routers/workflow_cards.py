"""Authenticated workflow-card draft, validation and publishing endpoints."""

from __future__ import annotations

from typing import Optional

from fastapi import APIRouter, Depends, Header, HTTPException, Query
from sqlmodel import Session, select

from api.database import get_session
from api.models import WorkflowCard, WorkflowCardVersion
from api.services.workflows.card_service import (
    card_payload,
    create_card,
    owned_card,
    publish_card,
    update_card,
    validate_card,
    version_payload,
)
from api.services.workflows.compiler import WorkflowValidationError
from api.services.workflows.schemas import CardCreate, CardUpdate, PublishRequest
from api.core.settings import settings
from .auth import get_current_user


def _require_enabled() -> None:
    if not settings.workflow_cards_enabled:
        raise HTTPException(status_code=404, detail="workflow cards are disabled")


router = APIRouter(dependencies=[Depends(_require_enabled)])
PREFIX = "/api/workflow-cards"


def _validation_error(exc: WorkflowValidationError) -> HTTPException:
    return HTTPException(
        status_code=422,
        detail={"code": "CARD_VALIDATION_FAILED", "errors": exc.errors, "warnings": exc.warnings},
    )


@router.get("")
def list_cards(
    status: Optional[str] = Query(default=None),
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
    session: Session = Depends(get_session),
    authorization: str = Header(None),
):
    user = get_current_user(authorization, session)
    statement = select(WorkflowCard).where(
        WorkflowCard.user_id == user.id,
        WorkflowCard.deleted_at.is_(None),
    )
    if status:
        statement = statement.where(WorkflowCard.status == status)
    rows = session.exec(statement.order_by(WorkflowCard.updated_at.desc()).offset(offset).limit(limit)).all()
    return {"items": [card_payload(row) for row in rows], "limit": limit, "offset": offset}


@router.post("", status_code=201)
def create(
    body: CardCreate,
    session: Session = Depends(get_session),
    authorization: str = Header(None),
):
    user = get_current_user(authorization, session)
    return card_payload(create_card(session, user.id, body))


@router.get("/{card_id}")
def get_card(
    card_id: str,
    session: Session = Depends(get_session),
    authorization: str = Header(None),
):
    user = get_current_user(authorization, session)
    row = owned_card(session, user.id, card_id)
    if not row:
        raise HTTPException(status_code=404, detail={"code": "CARD_NOT_FOUND"})
    return card_payload(row)


@router.patch("/{card_id}")
def patch_card(
    card_id: str,
    body: CardUpdate,
    session: Session = Depends(get_session),
    authorization: str = Header(None),
):
    user = get_current_user(authorization, session)
    row = owned_card(session, user.id, card_id)
    if not row:
        raise HTTPException(status_code=404, detail={"code": "CARD_NOT_FOUND"})
    return card_payload(update_card(session, row, body))


@router.post("/{card_id}/validate")
def validate(
    card_id: str,
    session: Session = Depends(get_session),
    authorization: str = Header(None),
):
    user = get_current_user(authorization, session)
    row = owned_card(session, user.id, card_id)
    if not row:
        raise HTTPException(status_code=404, detail={"code": "CARD_NOT_FOUND"})
    try:
        return validate_card(row)
    except WorkflowValidationError as exc:
        raise _validation_error(exc)


@router.post("/{card_id}/publish")
def publish(
    card_id: str,
    body: PublishRequest,
    session: Session = Depends(get_session),
    authorization: str = Header(None),
):
    user = get_current_user(authorization, session)
    row = owned_card(session, user.id, card_id)
    if not row:
        raise HTTPException(status_code=404, detail={"code": "CARD_NOT_FOUND"})
    try:
        version = publish_card(session, row, user.id, device_id=body.device_id)
    except WorkflowValidationError as exc:
        raise _validation_error(exc)
    return version_payload(version, include_definition=True)


@router.get("/{card_id}/versions")
def versions(
    card_id: str,
    session: Session = Depends(get_session),
    authorization: str = Header(None),
):
    user = get_current_user(authorization, session)
    row = owned_card(session, user.id, card_id)
    if not row:
        raise HTTPException(status_code=404, detail={"code": "CARD_NOT_FOUND"})
    items = session.exec(
        select(WorkflowCardVersion)
        .where(WorkflowCardVersion.card_id == row.id)
        .order_by(WorkflowCardVersion.version_number.desc())
    ).all()
    return {"items": [version_payload(item) for item in items]}


@router.delete("/{card_id}", status_code=204)
def archive(
    card_id: str,
    session: Session = Depends(get_session),
    authorization: str = Header(None),
):
    import time

    user = get_current_user(authorization, session)
    row = owned_card(session, user.id, card_id)
    if not row:
        raise HTTPException(status_code=404, detail={"code": "CARD_NOT_FOUND"})
    row.status = "archived"
    row.deleted_at = time.time()
    row.updated_at = row.deleted_at
    session.add(row)
    session.commit()
    return None
