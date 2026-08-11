"""Inbox persistence and device-safe event payloads for user notifications."""

from __future__ import annotations

import json
import time
import uuid
from typing import Any, Dict, Iterable, List, Optional, Tuple

from sqlmodel import Session, select

from api.models import AssistantAIConfig
from api.models.user_notification import UserNotification


def notification_room(user_id: int) -> str:
    return f"user_notifications_{int(user_id)}"


def _clean_text(value: object, limit: int) -> str:
    return " ".join(str(value or "").split())[:limit]


def _safe_attachments(values: Iterable[Dict[str, Any]]) -> List[Dict[str, Any]]:
    safe: List[Dict[str, Any]] = []
    for raw in list(values)[:5]:
        if not isinstance(raw, dict):
            continue
        safe.append({
            "file_ref": _clean_text(raw.get("file_ref"), 80),
            "file_name": _clean_text(raw.get("file_name"), 180),
            "mime_type": _clean_text(raw.get("mime_type"), 120) or "application/octet-stream",
            "bytes": max(0, int(raw.get("bytes") or 0)),
            "available": bool(raw.get("file_ref")),
        })
    return safe


def create_notification(
    session: Session,
    *,
    user_id: int,
    ai_config_id: Optional[int],
    body: str,
    attachments: Iterable[Dict[str, Any]] = (),
    app_push_required: bool,
    external_channel: str = "",
    external_delivered: bool = False,
) -> UserNotification:
    actor = session.get(AssistantAIConfig, ai_config_id) if ai_config_id else None
    actor_name = _clean_text(actor.name if actor else "数字成员", 80) or "数字成员"
    item = UserNotification(
        id=f"notice_{uuid.uuid4().hex}",
        user_id=int(user_id),
        ai_config_id=int(ai_config_id) if ai_config_id else None,
        title=f"{actor_name}发来消息",
        body=_clean_text(body, 4000),
        attachments_json=json.dumps(_safe_attachments(attachments), ensure_ascii=False),
        app_push_required=bool(app_push_required),
        push_status="pending" if app_push_required else "not_required",
        push_next_attempt_at=time.time() if app_push_required else 0.0,
        external_channel=_clean_text(external_channel, 32),
        external_delivered=bool(external_delivered),
        source="message.send+to",
    )
    session.add(item)
    session.commit()
    session.refresh(item)
    return item


def _attachments(item: UserNotification) -> List[Dict[str, Any]]:
    try:
        raw = json.loads(item.attachments_json or "[]")
    except (TypeError, ValueError):
        raw = []
    return _safe_attachments(raw if isinstance(raw, list) else [])


def notification_payload(item: UserNotification, *, device_safe: bool = False) -> Dict[str, Any]:
    attachments = _attachments(item)
    payload = {
        "notification_id": item.id,
        "user_id": item.user_id,
        "kind": item.kind,
        "title": item.title,
        "body": item.body,
        "severity": item.severity,
        "status": item.status,
        "action_url": item.action_url,
        "app_push_required": item.app_push_required,
        "external_channel": item.external_channel,
        "external_delivered": item.external_delivered,
        "attachment_count": len(attachments),
        "created_at": item.created_at,
        "updated_at": item.updated_at,
        "read_at": item.read_at,
    }
    if not device_safe:
        payload["attachments"] = attachments
    return payload


def list_notifications(
    session: Session,
    *,
    user_id: int,
    unread_only: bool = False,
    limit: int = 100,
) -> List[Dict[str, Any]]:
    query = select(UserNotification).where(UserNotification.user_id == int(user_id))
    if unread_only:
        query = query.where(UserNotification.status == "unread")
    rows = session.exec(
        query.order_by(UserNotification.created_at.desc()).limit(max(1, min(limit, 200)))
    ).all()
    return [notification_payload(item) for item in rows]


def pending_device_notifications(
    session: Session,
    *,
    user_id: int,
    limit: int = 100,
) -> List[Dict[str, Any]]:
    rows = session.exec(
        select(UserNotification).where(
            UserNotification.user_id == int(user_id),
            UserNotification.status == "unread",
            UserNotification.app_push_required.is_(True),
        ).order_by(UserNotification.created_at).limit(max(1, min(limit, 200)))
    ).all()
    return [notification_payload(item, device_safe=True) for item in rows]


def notification_events_since(
    session: Session,
    *,
    since: float,
    limit: int = 200,
) -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]]]:
    created = session.exec(
        select(UserNotification).where(
            UserNotification.created_at >= float(since),
            UserNotification.status == "unread",
            UserNotification.app_push_required.is_(True),
        ).order_by(UserNotification.created_at).limit(limit)
    ).all()
    resolved = session.exec(
        select(UserNotification).where(
            UserNotification.read_at.is_not(None),
            UserNotification.read_at >= float(since),
        ).order_by(UserNotification.read_at).limit(limit)
    ).all()
    return (
        [notification_payload(item, device_safe=True) for item in created],
        [{"notification_id": item.id, "user_id": item.user_id, "status": item.status} for item in resolved],
    )


def mark_read(session: Session, *, user_id: int, notification_id: str) -> Optional[UserNotification]:
    item = session.get(UserNotification, notification_id)
    if not item or item.user_id != int(user_id):
        return None
    if item.status == "unread":
        now = time.time()
        item.status = "read"
        item.read_at = now
        item.updated_at = now
        if item.push_status in {"pending", "retry", "sending"}:
            item.push_status = "cancelled"
            item.push_lease_owner = ""
            item.push_lease_expires_at = 0.0
        session.add(item)
        session.commit()
        session.refresh(item)
    return item


def mark_all_read(session: Session, *, user_id: int) -> int:
    rows = session.exec(select(UserNotification).where(
        UserNotification.user_id == int(user_id),
        UserNotification.status == "unread",
    )).all()
    now = time.time()
    for item in rows:
        item.status = "read"
        item.read_at = now
        item.updated_at = now
        if item.push_status in {"pending", "retry", "sending"}:
            item.push_status = "cancelled"
            item.push_lease_owner = ""
            item.push_lease_expires_at = 0.0
        session.add(item)
    if rows:
        session.commit()
    return len(rows)
