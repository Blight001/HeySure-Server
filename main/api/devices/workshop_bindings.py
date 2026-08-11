"""Read/write helpers for AI → built-in device bindings.

内置设备与 AI 支持多对多绑定。与端侧设备绑定（``api.devices.bindings``）的差异
仅在绑定方向：内置设备绑定从 AI 侧声明、存兼容表 ``WorkshopAiBinding``。
Shared by the dispatch path (which resolves the workshop agent for a
calling AI) and the REST binding endpoints.
"""

import time
from typing import List, Optional, Set

from sqlmodel import Session, select

from ..database import engine
from ..models import WorkshopAiBinding


def _coerce_int(value) -> Optional[int]:
    try:
        if value in (None, "", 0, "0"):
            return None
        return int(value)
    except (TypeError, ValueError):
        return None


def workshop_device_ids_for_config(user_id, ai_config_id) -> List[str]:
    """Workshop agent ids this AI is bound to (may be offline)."""
    uid = _coerce_int(user_id)
    cfg = _coerce_int(ai_config_id)
    if uid is None or cfg is None:
        return []
    with Session(engine) as session:
        rows = session.exec(
            select(WorkshopAiBinding).where(
                WorkshopAiBinding.user_id == uid,
                WorkshopAiBinding.ai_config_id == cfg,
            )
        ).all()
        return sorted({str(row.device_id or "").strip() for row in rows if str(row.device_id or "").strip()})


def bound_config_ids_for_agent(user_id, device_id) -> Set[int]:
    """AI config ids bound to one built-in device."""
    uid = _coerce_int(user_id)
    aid = str(device_id or "").strip()
    if uid is None or not aid:
        return set()
    with Session(engine) as session:
        rows = session.exec(
            select(WorkshopAiBinding).where(
                WorkshopAiBinding.user_id == uid,
                WorkshopAiBinding.device_id == aid,
            )
        ).all()
        return {int(row.ai_config_id) for row in rows if row.ai_config_id}


def bound_config_id_for_agent(user_id, device_id) -> Optional[int]:
    """Compatibility projection: the first bound AI config id."""
    ids = sorted(bound_config_ids_for_agent(user_id, device_id))
    return ids[0] if ids else None


def set_workshop_binding(user_id, device_id, ai_config_id, *, bound: bool, single: bool = False) -> bool:
    """Create or remove the (agent, AI) binding. Returns the stored state.

    Normal behavior is multi-bind: only this ``(device, AI)`` pair changes.
    ``single=True`` remains solely as a legacy replacement option.
    """
    uid = _coerce_int(user_id)
    aid = str(device_id or "").strip()
    cfg = _coerce_int(ai_config_id)
    if uid is None or not aid or cfg is None:
        return False
    with Session(engine) as session:
        rows = session.exec(
            select(WorkshopAiBinding).where(
                WorkshopAiBinding.user_id == uid,
                WorkshopAiBinding.device_id == aid,
            )
        ).all()
        current = next((row for row in rows if _coerce_int(row.ai_config_id) == cfg), None)
        if bound:
            dirty = False
            if single:
                for row in rows:
                    if row is not current:
                        session.delete(row)
                        dirty = True
            if not current:
                session.add(WorkshopAiBinding(user_id=uid, device_id=aid, ai_config_id=cfg))
                dirty = True
            else:
                current.updated_at = time.time()
                session.add(current)
                dirty = True
            if dirty:
                session.commit()
            return True
        if current:
            session.delete(current)
        if current:
            session.commit()
        return False


def config_bound_to_device(user_id, ai_config_id, device_id) -> bool:
    """该 AI 是否绑定到指定设备（按确切 device_id 判定）。"""
    return str(device_id or "").strip() in set(workshop_device_ids_for_config(user_id, ai_config_id))


def config_bound_to_library(user_id, ai_config_id) -> bool:
    from library.engine import device_id_for_user

    return config_bound_to_device(user_id, ai_config_id, device_id_for_user(user_id))

