"""Read/write helpers for persistent device → AI bindings.

Kept separate from the socket/REST layers so both the ``device:register``
handler (re-apply on connect) and the Workshop bind endpoint (operator
assigns) share one source of truth. See ``api.models.device_binding``.
"""

import time
from typing import Dict, Iterable, List, Optional

from sqlmodel import Session, select

from ..database import engine
from ..models import DeviceAiBinding


def _coerce_int(value) -> Optional[int]:
    try:
        if value in (None, "", 0, "0"):
            return None
        return int(value)
    except (TypeError, ValueError):
        return None


def _load_rows(session: Session, user_id: int, device_id: str):
    return session.exec(
        select(DeviceAiBinding)
        .where(
            DeviceAiBinding.user_id == user_id,
            DeviceAiBinding.device_id == device_id,
        )
        .order_by(DeviceAiBinding.ai_config_id.asc(), DeviceAiBinding.id.asc())
    ).all()


def get_bindings(user_id, device_id) -> List[int]:
    """Return every AI member assigned to one physical endpoint."""
    uid = _coerce_int(user_id)
    aid = str(device_id or "").strip()
    if uid is None or not aid:
        return []
    with Session(engine) as session:
        return sorted(
            {
                cfg
                for row in _load_rows(session, uid, aid)
                if (cfg := _coerce_int(row.ai_config_id)) is not None
            }
        )


def bindings_by_device_for_user(user_id) -> Dict[str, List[int]]:
    """Return all physical endpoint bindings grouped by device id."""
    uid = _coerce_int(user_id)
    if uid is None:
        return {}
    with Session(engine) as session:
        rows = session.exec(
            select(DeviceAiBinding).where(DeviceAiBinding.user_id == uid)
        ).all()
    grouped: Dict[str, set[int]] = {}
    for row in rows:
        device_id = str(row.device_id or "").strip()
        cfg = _coerce_int(row.ai_config_id)
        if device_id and cfg is not None:
            grouped.setdefault(device_id, set()).add(cfg)
    return {device_id: sorted(values) for device_id, values in grouped.items()}


def device_ids_for_config(user_id, ai_config_id) -> List[str]:
    """Return physical endpoint ids assigned to one owned AI config."""
    uid = _coerce_int(user_id)
    cfg = _coerce_int(ai_config_id)
    if uid is None or cfg is None:
        return []
    with Session(engine) as session:
        rows = session.exec(
            select(DeviceAiBinding).where(
                DeviceAiBinding.user_id == uid,
                DeviceAiBinding.ai_config_id == cfg,
            )
        ).all()
    return sorted({str(row.device_id or "").strip() for row in rows if str(row.device_id or "").strip()})


def get_binding(user_id, device_id) -> Optional[int]:
    """Compatibility projection: return the first assigned AI member."""
    bindings = get_bindings(user_id, device_id)
    return bindings[0] if bindings else None


def replace_bindings(user_id, device_id, ai_config_ids: Iterable[object]) -> List[int]:
    """Replace all assignments for one endpoint with the requested member set."""
    uid = _coerce_int(user_id)
    aid = str(device_id or "").strip()
    desired = sorted(
        {cfg for value in (ai_config_ids or []) if (cfg := _coerce_int(value)) is not None}
    )
    if uid is None or not aid:
        return []
    now = time.time()
    with Session(engine) as session:
        rows = _load_rows(session, uid, aid)
        kept = set()
        for row in rows:
            cfg = _coerce_int(row.ai_config_id)
            if cfg in desired and cfg not in kept:
                kept.add(cfg)
                continue
            session.delete(row)
        for cfg in desired:
            if cfg not in kept:
                session.add(
                    DeviceAiBinding(
                        user_id=uid,
                        device_id=aid,
                        ai_config_id=cfg,
                        updated_at=now,
                    )
                )
        session.commit()
    return desired


def set_member_binding(user_id, device_id, ai_config_id, *, bound: bool) -> List[int]:
    """Add or remove one AI member without disturbing the device's other members."""
    uid = _coerce_int(user_id)
    aid = str(device_id or "").strip()
    cfg = _coerce_int(ai_config_id)
    if uid is None or not aid or cfg is None:
        return []
    with Session(engine) as session:
        rows = session.exec(
            select(DeviceAiBinding)
            .where(
                DeviceAiBinding.user_id == uid,
                DeviceAiBinding.device_id == aid,
                DeviceAiBinding.ai_config_id == cfg,
            )
            .order_by(DeviceAiBinding.id.asc())
        ).all()
        if bound:
            if not rows:
                session.add(DeviceAiBinding(user_id=uid, device_id=aid, ai_config_id=cfg))
            for stale in rows[1:]:
                session.delete(stale)
        else:
            for row in rows:
                session.delete(row)
        session.commit()
    return get_bindings(uid, aid)


def set_binding(user_id, device_id, ai_config_id) -> Optional[int]:
    """Legacy single-select API: replace all assignments with zero or one AI.

    Returns the stored ai_config_id (or None when unassigned).
    """
    cfg = _coerce_int(ai_config_id)
    stored = replace_bindings(user_id, device_id, [cfg] if cfg is not None else [])
    if not stored:
        return None
    return cfg
