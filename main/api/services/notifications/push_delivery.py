"""Durable endpoint registration and lease-based user push delivery state."""

from __future__ import annotations

import time
import uuid
from typing import List, Optional

from sqlalchemy import and_, or_
from sqlmodel import Session, select

from api.models.user_notification import UserNotification
from api.models.user_push_endpoint import UserPushEndpoint


MAX_PUSH_ATTEMPTS = 5
PUSH_LEASE_SECONDS = 60.0


def _text(value: object, limit: int) -> str:
    return " ".join(str(value or "").split())[:limit]


def endpoint_metadata(item: UserPushEndpoint) -> dict:
    return {
        "endpoint_id": item.id,
        "provider": item.provider,
        "device_id": item.device_id,
        "app_version": item.app_version,
        "enabled": item.enabled,
        "last_seen_at": item.last_seen_at,
    }


def upsert_endpoint(
    session: Session,
    *,
    user_id: int,
    provider: str,
    device_id: str,
    push_token: str,
    app_version: str = "",
) -> UserPushEndpoint:
    provider = _text(provider, 32).lower()
    device_id = _text(device_id, 160)
    push_token = str(push_token or "").strip()[:4096]
    if provider != "huawei" or not device_id or not push_token:
        raise ValueError("invalid push endpoint")
    item = session.exec(select(UserPushEndpoint).where(
        UserPushEndpoint.provider == provider,
        UserPushEndpoint.device_id == device_id,
    )).first()
    now = time.time()
    if not item:
        item = UserPushEndpoint(
            id=f"push_{uuid.uuid4().hex}",
            user_id=int(user_id),
            provider=provider,
            device_id=device_id,
            push_token=push_token,
            created_at=now,
        )
    item.user_id = int(user_id)
    item.push_token = push_token
    item.app_version = _text(app_version, 40)
    item.enabled = True
    item.updated_at = now
    item.last_seen_at = now
    session.add(item)
    session.commit()
    session.refresh(item)
    return item


def disable_endpoint(
    session: Session, *, user_id: int, provider: str, device_id: str,
) -> Optional[UserPushEndpoint]:
    item = session.exec(select(UserPushEndpoint).where(
        UserPushEndpoint.user_id == int(user_id),
        UserPushEndpoint.provider == _text(provider, 32).lower(),
        UserPushEndpoint.device_id == _text(device_id, 160),
    )).first()
    if not item:
        return None
    item.enabled = False
    item.updated_at = time.time()
    session.add(item)
    session.commit()
    session.refresh(item)
    return item


def active_endpoints(session: Session, *, user_id: int, provider: str) -> List[UserPushEndpoint]:
    return list(session.exec(select(UserPushEndpoint).where(
        UserPushEndpoint.user_id == int(user_id),
        UserPushEndpoint.provider == provider,
        UserPushEndpoint.enabled.is_(True),
    )).all())


def claim_notifications(
    session: Session, *, owner: str, limit: int = 20, now: Optional[float] = None,
) -> List[UserNotification]:
    now = float(now or time.time())
    due = or_(
        and_(
            UserNotification.push_status.in_(["pending", "retry"]),
            UserNotification.push_next_attempt_at <= now,
        ),
        and_(
            UserNotification.push_status == "sending",
            UserNotification.push_lease_expires_at <= now,
        ),
    )
    rows = session.exec(
        select(UserNotification)
        .where(
            UserNotification.status == "unread",
            UserNotification.app_push_required.is_(True),
            due,
        )
        .order_by(UserNotification.push_next_attempt_at, UserNotification.created_at)
        .limit(max(1, min(limit, 100)))
        .with_for_update(skip_locked=True)
    ).all()
    for item in rows:
        item.push_status = "sending"
        item.push_attempts += 1
        item.push_lease_owner = _text(owner, 160)
        item.push_lease_expires_at = now + PUSH_LEASE_SECONDS
        item.updated_at = now
        session.add(item)
    if rows:
        session.commit()
        for item in rows:
            session.refresh(item)
    return list(rows)


def complete_delivery(
    session: Session,
    *,
    notification_id: str,
    owner: str,
    delivered: bool,
    error_code: str = "",
) -> Optional[UserNotification]:
    item = session.get(UserNotification, notification_id)
    if not item or item.push_status != "sending" or item.push_lease_owner != owner:
        return None
    now = time.time()
    if delivered:
        item.push_status = "delivered"
        item.push_delivered_at = now
        item.push_last_error_code = ""
    else:
        terminal = item.push_attempts >= MAX_PUSH_ATTEMPTS
        item.push_status = "failed" if terminal else "retry"
        delay = min(900.0, 5.0 * (3 ** max(0, item.push_attempts - 1)))
        item.push_next_attempt_at = now + delay
        item.push_last_error_code = _text(error_code, 80) or "push_failed"
    item.push_lease_owner = ""
    item.push_lease_expires_at = 0.0
    item.updated_at = now
    session.add(item)
    session.commit()
    session.refresh(item)
    return item


def release_without_endpoint(
    session: Session, *, notification_id: str, owner: str,
) -> Optional[UserNotification]:
    item = session.get(UserNotification, notification_id)
    if not item or item.push_status != "sending" or item.push_lease_owner != owner:
        return None
    now = time.time()
    item.push_status = "pending"
    item.push_attempts = max(0, item.push_attempts - 1)
    item.push_next_attempt_at = now + 60.0
    item.push_lease_owner = ""
    item.push_lease_expires_at = 0.0
    item.push_last_error_code = "no_endpoint"
    item.updated_at = now
    session.add(item)
    session.commit()
    session.refresh(item)
    return item
