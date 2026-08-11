"""Connector-owned durable delivery loop for user operating-system pushes."""

from __future__ import annotations

import asyncio
import logging
import socket
import uuid

from sqlmodel import Session

from api.database import engine
from api.core.settings import settings
from api.services.notifications import huawei_push
from api.services.notifications.push_delivery import (
    active_endpoints,
    claim_notifications,
    complete_delivery,
    release_without_endpoint,
)
from api.services.notifications.user_notifications import notification_payload


logger = logging.getLogger(__name__)
PUSH_OWNER = f"push-{socket.gethostname()}-{uuid.uuid4().hex[:12]}"


async def process_pending_user_pushes(limit: int = 20) -> int:
    if not huawei_push.is_configured():
        return 0
    with Session(engine) as session:
        rows = claim_notifications(session, owner=PUSH_OWNER, limit=limit)
    delivered = 0
    for item in rows:
        with Session(engine) as session:
            endpoints = active_endpoints(session, user_id=item.user_id, provider="huawei")
        if not endpoints:
            with Session(engine) as session:
                release_without_endpoint(session, notification_id=item.id, owner=PUSH_OWNER)
            continue
        result = await huawei_push.send_notification(
            [endpoint.push_token for endpoint in endpoints],
            notification_payload(item, device_safe=True),
        )
        with Session(engine) as session:
            complete_delivery(
                session,
                notification_id=item.id,
                owner=PUSH_OWNER,
                delivered=result.delivered,
                error_code=result.error_code,
            )
        if result.delivered:
            delivered += 1
        else:
            logger.warning("HMS push delivery failed code=%s", result.error_code)
    return delivered


async def run_user_push_scheduler(stop_event: asyncio.Event) -> None:
    """Run OS push delivery independently of workflow-card feature flags."""
    interval = float(settings.huawei_push_poll_interval_seconds)
    while not stop_event.is_set():
        try:
            await process_pending_user_pushes()
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("user push scheduler iteration failed")
        try:
            await asyncio.wait_for(stop_event.wait(), timeout=interval)
        except asyncio.TimeoutError:
            continue
