"""Authenticated user-facing workflow confirmation notification routes."""

from fastapi import APIRouter, Depends, Header, HTTPException, Query
from sqlmodel import Session

from api.core.settings import settings
from api.database import get_session
from api.services.workflows.confirmation_notifications import pending_notifications

from .auth import get_current_user


def _require_enabled() -> None:
    if not settings.workflow_cards_enabled:
        raise HTTPException(status_code=404, detail="workflow cards are disabled")


router = APIRouter(dependencies=[Depends(_require_enabled)])
PREFIX = "/api"


@router.get("/workflow-confirmations/pending")
def pending_confirmation_list(
    limit: int = Query(default=100, ge=1, le=200),
    session: Session = Depends(get_session),
    authorization: str = Header(None),
):
    user = get_current_user(authorization, session)
    return {"items": pending_notifications(session, user_id=user.id, limit=limit)}
