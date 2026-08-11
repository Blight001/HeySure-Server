"""数字社会设备建筑的用户自定义顺序。"""

import time

from sqlmodel import Session, select

from api.models import WorldDeviceMeta


MAX_WORLD_DEVICES = 256


def normalized_device_ids(device_ids: list[str]) -> list[str]:
    normalized = [str(device_id or "").strip() for device_id in device_ids]
    if any(not device_id for device_id in normalized):
        raise ValueError("设备标识不能为空")
    if len(normalized) > MAX_WORLD_DEVICES:
        raise ValueError(f"设备数量不能超过 {MAX_WORLD_DEVICES}")
    if len(set(normalized)) != len(normalized):
        raise ValueError("设备顺序中存在重复设备")
    return normalized


def list_world_device_order(session: Session, user_id: int) -> list[str]:
    rows = session.exec(
        select(WorldDeviceMeta)
        .where(WorldDeviceMeta.user_id == user_id)
        .order_by(WorldDeviceMeta.sort_order, WorldDeviceMeta.device_id)
    ).all()
    return [row.device_id for row in rows]


def save_world_device_order(session: Session, user_id: int, device_ids: list[str]) -> list[str]:
    normalized = normalized_device_ids(device_ids)
    rows = session.exec(
        select(WorldDeviceMeta).where(WorldDeviceMeta.user_id == user_id)
    ).all()
    by_device = {row.device_id: row for row in rows}
    desired = set(normalized)
    now = time.time()
    for sort_order, device_id in enumerate(normalized):
        row = by_device.get(device_id)
        if row is None:
            row = WorldDeviceMeta(user_id=user_id, device_id=device_id)
        row.sort_order = sort_order
        row.updated_at = now
        session.add(row)
    # 暂时离线、未出现在本次拖拽列表中的设备保留相对顺序，但统一排到当前设备之后，
    # 避免与新序号碰撞后在重连时意外插回用户刚排好的队列中间。
    stale_rows = sorted(
        (row for row in rows if row.device_id not in desired),
        key=lambda row: (row.sort_order, row.device_id),
    )
    for offset, row in enumerate(stale_rows, start=len(normalized)):
        row.sort_order = offset
        row.updated_at = now
        session.add(row)
    session.commit()
    return normalized
