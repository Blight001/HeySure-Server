"""Read-only admin audit log route."""

from fastapi import APIRouter, Depends
from sqlmodel import Session, select

from api.database import get_session
from api.models import AdminAuditLog, User
from gateway.routers.admin import require_admin_user


router = APIRouter()
PREFIX = "/api/admin"


def serialize_audit_entry(row: AdminAuditLog) -> dict:
    return {
        "id": row.id,
        "created_at": row.created_at,
        "actor_id": row.actor_id,
        "actor_account": row.actor_account,
        "action": row.action,
        "target_type": row.target_type,
        "target_id": row.target_id,
        "target_label": row.target_label,
        "detail": row.detail,
    }


@router.get("/audit")
def list_audit(
    limit: int = 100,
    session: Session = Depends(get_session),
    _admin: User = Depends(require_admin_user),
) -> dict:
    limit = max(1, min(500, int(limit or 100)))
    rows = session.exec(
        select(AdminAuditLog).order_by(AdminAuditLog.created_at.desc()).limit(limit)
    ).all()
    return {"entries": [serialize_audit_entry(row) for row in rows]}
