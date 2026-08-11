"""DB-backed endpoint routing projections for multi-AI device bindings."""

from typing import List, Set, Tuple

from sqlmodel import Session, select

from ..database import engine
from ..models import DevicePresence
from .bindings import device_ids_for_config


def online_devices_for_config(user_id, ai_config_id) -> List[Tuple[str, str, Set[str]]]:
    """Return online endpoint capabilities for every device bound to one AI."""
    try:
        uid, config_id = int(user_id), int(ai_config_id)
    except (TypeError, ValueError):
        return []
    device_ids = device_ids_for_config(uid, config_id)
    if uid <= 0 or config_id <= 0 or not device_ids:
        return []
    with Session(engine) as session:
        rows = session.exec(
            select(DevicePresence)
            .where(
                DevicePresence.user_id == uid,
                DevicePresence.device_id.in_(device_ids),
                DevicePresence.online == True,  # noqa: E712
            )
            .order_by(DevicePresence.updated_at.desc(), DevicePresence.id.desc())
        ).all()
    from .presence import _decode, mcp_capabilities

    result = []
    seen = set()
    for row in rows:
        device_id = str(row.device_id or "").strip()
        if device_id and device_id not in seen:
            seen.add(device_id)
            result.append((device_id, str(row.device_type or "").strip(), mcp_capabilities(_decode(row))))
    return result
