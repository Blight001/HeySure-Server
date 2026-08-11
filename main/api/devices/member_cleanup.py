"""Cleanup helpers for deleting one AI from multi-member device bindings."""

import time
from typing import Iterable, Mapping

from sqlmodel import Session, select

from ..models import DeviceAiBinding, DevicePresence, DeviceTypeMcpPermission
from .bindings import get_bindings


def delete_member_bindings_and_scopes(session: Session, user_id: int, config_id: int) -> set[str]:
    bindings = session.exec(
        select(DeviceAiBinding).where(
            DeviceAiBinding.user_id == user_id,
            DeviceAiBinding.ai_config_id == config_id,
        )
    ).all()
    device_ids = {str(row.device_id or "").strip() for row in bindings}
    for row in bindings:
        session.delete(row)
    scopes = session.exec(
        select(DeviceTypeMcpPermission).where(
            DeviceTypeMcpPermission.user_id == user_id,
            DeviceTypeMcpPermission.ai_config_id == config_id,
        )
    ).all()
    for row in scopes:
        session.delete(row)
    return {device_id for device_id in device_ids if device_id}


def refresh_presence_primaries(session: Session, user_id: int, device_ids: Iterable[str]) -> None:
    for device_id in device_ids:
        bindings = session.exec(
            select(DeviceAiBinding).where(
                DeviceAiBinding.user_id == user_id,
                DeviceAiBinding.device_id == device_id,
            )
        ).all()
        primary = min((int(row.ai_config_id) for row in bindings if row.ai_config_id), default=None)
        rows = session.exec(
            select(DevicePresence).where(
                DevicePresence.user_id == user_id,
                DevicePresence.device_id == device_id,
            )
        ).all()
        for row in rows:
            row.ai_config_id = primary
            row.updated_at = time.time()
            session.add(row)


def sync_live_agent_bindings(
    live_agents: Mapping[str, dict],
    user_id: int,
    deleted_config_id: int,
) -> bool:
    changed = False
    for agent in live_agents.values():
        if agent.get("userId") != user_id:
            continue
        previous = {
            int(value) for value in (agent.get("boundAiConfigIds") or []) if str(value).isdigit()
        }
        legacy_id = agent.get("aiConfigId") or agent.get("ai_config_id")
        if str(legacy_id or "").isdigit():
            previous.add(int(legacy_id))
        if deleted_config_id not in previous:
            continue
        bound_ids = get_bindings(user_id, agent.get("id"))
        agent["boundAiConfigIds"] = bound_ids
        agent["aiConfigId"] = bound_ids[0] if bound_ids else None
        changed = True
    return changed
