"""Authenticated workflow-card draft, validation and publishing endpoints."""

from __future__ import annotations

from typing import Optional

from fastapi import APIRouter, Depends, Header, HTTPException, Query
from sqlmodel import Session, select

from api.database import get_session
from api.models import DevicePresence, WorkflowCard, WorkflowCardVersion
from api.services.workflows.card_service import (
    card_payload,
    create_card,
    delete_card,
    owned_card,
    publish_card,
    update_card,
    validate_card,
    version_payload,
)
from api.services.workflows.compiler import WorkflowValidationError
from api.services.workflows.trace import definition_from_trace
from api.services.workflows.schemas import CardCreate, CardUpdate, PublishRequest, TraceDraftRequest
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
    tag: Optional[str] = Query(default=None),
    device_id: Optional[str] = Query(default=None),
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
    session: Session = Depends(get_session),
    authorization: str = Header(None),
):
    user = get_current_user(authorization, session)
    statement = select(WorkflowCard).where(
        WorkflowCard.user_id == user.id,
        WorkflowCard.deleted_at.is_(None),
        WorkflowCard.status != "archived",
    )
    if status:
        statement = statement.where(WorkflowCard.status == status)
    rows = session.exec(statement.order_by(WorkflowCard.updated_at.desc())).all()
    if tag:
        wanted = tag.strip().lower()
        rows = [row for row in rows if wanted in {str(item).lower() for item in card_payload(row)["tags"]}]
    if device_id:
        device = session.exec(select(DevicePresence).where(
            DevicePresence.user_id == user.id, DevicePresence.device_id == device_id
        )).first()
        if not device:
            rows = []
        else:
            compatible = []
            for row in rows:
                version = session.get(WorkflowCardVersion, row.latest_version_id) if row.latest_version_id else None
                contracts = version_payload(version).get("tool_contracts", {}) if version else {}
                if all(not item.get("provider") or item.get("provider") == device.device_type for item in contracts.values()):
                    compatible.append(row)
            rows = compatible
    total = len(rows)
    rows = rows[offset:offset + limit]
    return {"items": [card_payload(row) for row in rows], "limit": limit, "offset": offset, "total": total}


@router.post("", status_code=201)
def create(
    body: CardCreate,
    session: Session = Depends(get_session),
    authorization: str = Header(None),
):
    user = get_current_user(authorization, session)
    return card_payload(create_card(session, user.id, body))


@router.post("/import", status_code=201)
def import_card(
    body: CardCreate,
    session: Session = Depends(get_session),
    authorization: str = Header(None),
):
    user = get_current_user(authorization, session)
    # Imports are always drafts and must be validated/published locally so a
    # foreign contract snapshot can never bypass current device checks.
    return card_payload(create_card(session, user.id, body))


@router.post("/from-trace", status_code=201)
def create_from_trace(
    body: TraceDraftRequest,
    session: Session = Depends(get_session),
    authorization: str = Header(None),
):
    user = get_current_user(authorization, session)
    try:
        definition = definition_from_trace(body.calls, name=body.name, description=body.description)
    except WorkflowValidationError as exc:
        raise _validation_error(exc)
    card = CardCreate(
        name=body.name,
        description=body.description,
        tags=body.tags,
        risk_level=body.risk_level,
        definition=definition,
    )
    return card_payload(create_card(session, user.id, card))


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
        return validate_card(row, session)
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
        version = publish_card(
            session,
            row,
            user.id,
            device_id=body.device_id,
            device_ids=body.device_ids,
        )
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


@router.get("/{card_id}/versions/{version_id}")
def get_version(
    card_id: str,
    version_id: str,
    session: Session = Depends(get_session),
    authorization: str = Header(None),
):
    user = get_current_user(authorization, session)
    row = owned_card(session, user.id, card_id)
    if not row:
        raise HTTPException(status_code=404, detail={"code": "CARD_NOT_FOUND"})
    version = session.exec(
        select(WorkflowCardVersion).where(
            WorkflowCardVersion.id == version_id,
            WorkflowCardVersion.card_id == row.id,
        )
    ).first()
    if not version:
        raise HTTPException(status_code=404, detail={"code": "CARD_VERSION_NOT_FOUND"})
    return version_payload(version, include_definition=True)


@router.post("/{card_id}/clone", status_code=201)
def clone_card(
    card_id: str,
    session: Session = Depends(get_session),
    authorization: str = Header(None),
):
    user = get_current_user(authorization, session)
    row = owned_card(session, user.id, card_id)
    if not row:
        raise HTTPException(status_code=404, detail={"code": "CARD_NOT_FOUND"})
    source = card_payload(row)
    body = CardCreate(
        name=f"{row.name}（副本）",
        description=row.description,
        tags=source["tags"],
        risk_level=row.risk_level,
        definition=source["definition"],
    )
    return card_payload(create_card(session, user.id, body))


@router.get("/{card_id}/export")
def export_card(
    card_id: str,
    session: Session = Depends(get_session),
    authorization: str = Header(None),
):
    user = get_current_user(authorization, session)
    row = owned_card(session, user.id, card_id)
    if not row:
        raise HTTPException(status_code=404, detail={"code": "CARD_NOT_FOUND"})
    payload = card_payload(row)
    return {
        "schema": "heysure.workflow-card.export/v1",
        "name": payload["name"],
        "description": payload["description"],
        "tags": payload["tags"],
        "risk_level": payload["risk_level"],
        "definition": payload["definition"],
    }


@router.delete("/{card_id}", status_code=204)
def delete_card_route(
    card_id: str,
    session: Session = Depends(get_session),
    authorization: str = Header(None),
):
    user = get_current_user(authorization, session)
    row = owned_card(session, user.id, card_id)
    if not row:
        raise HTTPException(status_code=404, detail={"code": "CARD_NOT_FOUND"})
    delete_card(session, row)
    return None


@router.post("/{card_id}/deprecate")
def deprecate(
    card_id: str,
    session: Session = Depends(get_session),
    authorization: str = Header(None),
):
    user = get_current_user(authorization, session)
    row = owned_card(session, user.id, card_id)
    if not row:
        raise HTTPException(status_code=404, detail={"code": "CARD_NOT_FOUND"})
    if not row.latest_version_id:
        raise HTTPException(status_code=409, detail={"code": "CARD_VERSION_NOT_RUNNABLE"})
    row.status = "deprecated"
    import time
    row.updated_at = time.time()
    session.add(row)
    session.commit()
    session.refresh(row)
    return card_payload(row)
