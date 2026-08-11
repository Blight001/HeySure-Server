import pytest
from sqlalchemy.pool import StaticPool
from sqlmodel import Session, create_engine

from api.devices.world_order import list_world_device_order, save_world_device_order
from api.models import WorldDeviceMeta


def _memory_engine():
    db = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    WorldDeviceMeta.__table__.create(db)
    return db


def test_world_device_order_is_persistent_and_user_scoped():
    db = _memory_engine()
    with Session(db) as session:
        assert save_world_device_order(session, 1, ["phone-b", "desktop-a", "offline-c"]) == [
            "phone-b",
            "desktop-a",
            "offline-c",
        ]
        save_world_device_order(session, 2, ["browser-c"])
        save_world_device_order(session, 1, ["desktop-a", "phone-b"])

        assert list_world_device_order(session, 1) == ["desktop-a", "phone-b", "offline-c"]
        assert list_world_device_order(session, 2) == ["browser-c"]


def test_world_device_order_rejects_duplicate_devices():
    db = _memory_engine()
    with Session(db) as session, pytest.raises(ValueError, match="重复设备"):
        save_world_device_order(session, 1, ["phone-a", "phone-a"])
