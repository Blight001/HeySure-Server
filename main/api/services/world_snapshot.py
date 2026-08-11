"""数字社会首屏聚合所需的容错投影。"""

from sqlmodel import Session, select

from api.models import DevicePresence


def _offline_agent_item(row, bound_ids: list[int]) -> dict:
    device_id = str(row.device_id or "").strip()
    device_type = str(row.device_type or "").strip()
    try:
        from api.devices.presence import device_remark_value, effective_device_icon

        icon = effective_device_icon(row)
        remark = device_remark_value(getattr(row, "remark", ""))
        icon_override = str(getattr(row, "icon_override", "") or "").strip()
    except Exception:
        icon = str(getattr(row, "icon", "") or "").strip()
        remark = ""
        icon_override = ""
    return {
        "id": device_id,
        "name": str(row.name or "").strip() or device_id,
        "platform": str(row.platform or "").strip() or device_type,
        "deviceType": device_type,
        "icon": icon,
        "iconOverride": icon_override,
        "remark": remark,
        "isWindowsDesktop": device_type == "desktop",
        "isBrowserExtension": device_type == "browser",
        "isAndroid": device_type == "android",
        "aiConfigId": bound_ids[0] if bound_ids else row.ai_config_id,
        "boundAiConfigIds": bound_ids,
        "capabilities": [],
        "lifecycle": "offline",
        "online": False,
        "lastError": None,
    }


def append_bound_offline_agents(session: Session, user_id: int, agents: list[dict]) -> None:
    """世界保留已绑定离线设备，普通在线设备列表的合同不变。"""
    try:
        from api.devices.bindings import bindings_by_device_for_user

        bound_by_device = bindings_by_device_for_user(user_id)
        online_ids = {str(row.get("id") or row.get("deviceId") or "") for row in agents}
        rows = session.exec(
            select(DevicePresence).where(
                DevicePresence.user_id == user_id,
                DevicePresence.online == False,  # noqa: E712
                DevicePresence.ai_config_id.is_not(None),
            )
        ).all()
        for row in rows:
            device_id = str(row.device_id or "").strip()
            if device_id and device_id not in online_ids:
                agents.append(_offline_agent_item(row, bound_by_device.get(device_id, [])))
    except Exception:
        return


def load_knowledge_snapshot(user_id: int) -> tuple[list[dict], int]:
    from api.services.knowledge import librarian_service

    try:
        items = []
        for item in librarian_service.list_topics(user_id=user_id, status="active"):
            memory_id = str(item.get("memory_id") or "")
            if not memory_id:
                continue
            if memory_id.startswith("builtin."):
                items.append(item)
                continue
            try:
                items.append(librarian_service.read(user_id=user_id, memory_id=memory_id))
            except Exception:
                items.append(item)
        return items, len(items)
    except Exception:
        return [], 0


def load_pending_proposals(user_id: int) -> list[dict]:
    from api.services.knowledge import librarian_service

    try:
        return librarian_service.list_pending_for_review(user_id=user_id)
    except Exception:
        return []
