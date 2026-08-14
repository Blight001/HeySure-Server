"""User-scoped device catalog metadata overlay for Gateway inventory rows."""

from __future__ import annotations

from sqlmodel import Session, select

from api.database import engine
from api.models import DevicePresence

from .device_prompt import effective_ai_description


def _metadata(row: DevicePresence) -> dict:
    return {
        "reportedAiDescription": str(row.reported_ai_description or ""),
        "aiDescriptionOverride": str(row.ai_description_override or ""),
        "effectiveAiDescription": effective_ai_description(row),
        "catalogGeneration": int(row.catalog_generation or 0),
        "catalogHash": str(row.catalog_hash or ""),
        "catalogProtocolVersion": int(row.catalog_protocol_version or 1),
    }


def apply_capability_metadata_for_user(rows: list[dict], user_id: int) -> list[dict]:
    with Session(engine) as session:
        stored = session.exec(
            select(DevicePresence)
            .where(DevicePresence.user_id == int(user_id))
            .order_by(DevicePresence.updated_at.desc(), DevicePresence.id.desc())
        ).all()
    by_device: dict[str, dict] = {}
    for item in stored:
        device_id = str(item.device_id or "").strip()
        if device_id and device_id not in by_device:
            by_device[device_id] = _metadata(item)
    return [
        {**row, **by_device.get(str(row.get("id") or row.get("deviceId") or "").strip(), {})}
        for row in rows
    ]
