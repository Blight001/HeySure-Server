"""Authenticated workflow-card immutable-version endpoints."""

from __future__ import annotations

from typing import Any, Optional

from fastapi import APIRouter, Depends, Header, HTTPException, Query
from sqlmodel import Session, select

from api.database import get_session
from api.models import DevicePresence, WorkflowCard, WorkflowCardVersion
from api.services.workflows.card_service import (
    card_payload,
    create_card,
    delete_card,
    owned_card,
    update_card,
    update_editor_layout,
    validate_card,
    version_payload,
)
from api.services.workflows.compiler import WorkflowValidationError
from api.services.workflows.trace import definition_from_trace
from api.services.workflows.patch_service import patch_card_definition
from api.services.workflows.definition_replace_service import replace_card_definition
from api.services.workflows.preview_token import consume_preview_token
from api.services.workflows.schemas import (
    CardCreate,
    CardLayoutUpdate,
    CardUpdate,
    DefinitionPatchRequest,
    DefinitionReplaceRequest,
    TraceDraftRequest,
)
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


def _create_saved_card(session: Session, user_id: int, body: CardCreate):
    try:
        return card_payload(create_card(session, user_id, body))
    except WorkflowValidationError as exc:
        raise _validation_error(exc)


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
                contracts = version_payload(version, include_contracts=True).get("tool_contracts", {}) if version else {}
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
    return _create_saved_card(session, user.id, body)


@router.post("/import", status_code=201)
def import_card(
    body: CardCreate,
    session: Session = Depends(get_session),
    authorization: str = Header(None),
):
    user = get_current_user(authorization, session)
    # Import is persisted only after it compiles into an immutable version.
    return _create_saved_card(session, user.id, body)


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
    return _create_saved_card(session, user.id, card)


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
    try:
        return card_payload(update_card(session, row, body, user_id=user.id))
    except WorkflowValidationError as exc:
        raise _validation_error(exc)


def _owned_version(
    session: Session, row: WorkflowCard, version_id: Optional[str] = None,
) -> WorkflowCardVersion:
    selected_id = str(version_id or row.latest_version_id or "")
    version = session.exec(
        select(WorkflowCardVersion).where(
            WorkflowCardVersion.id == selected_id,
            WorkflowCardVersion.card_id == row.id,
        )
    ).first()
    if not version:
        raise HTTPException(status_code=404, detail={"code": "CARD_VERSION_NOT_FOUND"})
    return version


@router.put("/{card_id}/layout")
def save_card_layout(
    card_id: str,
    body: CardLayoutUpdate,
    session: Session = Depends(get_session),
    authorization: str = Header(None),
):
    user = get_current_user(authorization, session)
    row = owned_card(session, user.id, card_id)
    if not row:
        raise HTTPException(status_code=404, detail={"code": "CARD_NOT_FOUND"})
    try:
        return card_payload(update_editor_layout(session, row, body.positions))
    except WorkflowValidationError as exc:
        raise _validation_error(exc)


@router.post("/{card_id}/patch-definition")
def patch_definition(
    card_id: str,
    body: DefinitionPatchRequest,
    session: Session = Depends(get_session),
    authorization: str = Header(None),
):
    user = get_current_user(authorization, session)
    row = owned_card(session, user.id, card_id)
    if not row:
        raise HTTPException(status_code=404, detail={"code": "CARD_NOT_FOUND"})
    try:
        operations: Any = body.operations
        if body.preview_token and not body.dry_run:
            operations = consume_preview_token(
                body.preview_token,
                action="patch",
                user_id=user.id,
                card_id=row.id,
                base_version_id=body.base_version_id,
            )
        if not isinstance(operations, list):
            raise WorkflowValidationError(["patch requires operations or preview_token"])
        return patch_card_definition(
            session,
            card=row,
            user_id=user.id,
            base_version_id=body.base_version_id,
            operations=operations,
            dry_run=body.dry_run,
        )
    except WorkflowValidationError as exc:
        raise _validation_error(exc)


@router.post("/{card_id}/replace-definition")
def replace_definition(
    card_id: str,
    body: DefinitionReplaceRequest,
    session: Session = Depends(get_session),
    authorization: str = Header(None),
):
    user = get_current_user(authorization, session)
    row = owned_card(session, user.id, card_id)
    if not row:
        raise HTTPException(status_code=404, detail={"code": "CARD_NOT_FOUND"})
    try:
        definition: Any = body.definition
        if body.preview_token and not body.dry_run:
            definition = consume_preview_token(
                body.preview_token,
                action="replace_definition",
                user_id=user.id,
                card_id=row.id,
                base_version_id=body.base_version_id,
            )
        if not isinstance(definition, dict):
            raise WorkflowValidationError(["replace_definition requires definition or preview_token"])
        return replace_card_definition(
            session,
            card=row,
            user_id=user.id,
            base_version_id=body.base_version_id,
            definition=definition,
            dry_run=body.dry_run,
        )
    except WorkflowValidationError as exc:
        raise _validation_error(exc)


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


@router.get("/{card_id}/definition")
def get_definition(
    card_id: str,
    version_id: Optional[str] = Query(default=None),
    step_offset: int = Query(default=0, ge=0),
    step_limit: Optional[int] = Query(default=None, ge=1, le=100),
    session: Session = Depends(get_session),
    authorization: str = Header(None),
):
    """Return an immutable definition in full, or a deterministic page of its steps."""
    user = get_current_user(authorization, session)
    row = owned_card(session, user.id, card_id)
    if not row:
        raise HTTPException(status_code=404, detail={"code": "CARD_NOT_FOUND"})
    version = _owned_version(session, row, version_id)
    payload = version_payload(version, include_definition=True)
    definition = payload["definition"]
    steps = definition.get("steps") if isinstance(definition, dict) else None
    ordered = list(steps.items()) if isinstance(steps, dict) else []
    end = len(ordered) if step_limit is None else min(len(ordered), step_offset + step_limit)
    selected = ordered[step_offset:end]
    if step_offset or step_limit is not None:
        definition = dict(definition)
        definition["steps"] = dict(selected)
        payload["definition"] = definition
    payload["step_page"] = {
        "offset": step_offset,
        "limit": step_limit,
        "returned": len(selected),
        "total": len(ordered),
        "has_more": end < len(ordered),
        "next_offset": end if end < len(ordered) else None,
        "definition_complete": step_offset == 0 and end == len(ordered),
    }
    return payload


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
    version = _owned_version(session, row, version_id)
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
    latest = session.get(WorkflowCardVersion, row.latest_version_id) if row.latest_version_id else None
    definition = version_payload(latest, include_definition=True)["definition"] if latest else source["definition"]
    body = CardCreate(
        name=f"{row.name}（副本）",
        description=row.description,
        tags=source["tags"],
        access_scope=source["access_scope"],
        allowed_ai_config_ids=source["allowed_ai_config_ids"],
        risk_level=row.risk_level,
        definition=definition,
    )
    return _create_saved_card(session, user.id, body)


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
        "access_scope": payload["access_scope"],
        "allowed_ai_config_ids": payload["allowed_ai_config_ids"],
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
