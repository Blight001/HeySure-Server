import uuid

import pytest
from sqlalchemy.exc import IntegrityError
from sqlmodel import Session, select

from api.database import engine
from api.devices.mcp_permissions import get_scope, set_scope
from api.devices.world_order import list_world_device_order, save_world_device_order
from api.models import (
    AssistantAIConfig,
    DeviceAiBinding,
    DeviceTypeMcpPermission,
    User,
    WorldDeviceMeta,
)


pytestmark = pytest.mark.integration


def test_postgres_enforces_multi_bind_pairs_and_member_scope_isolation():
    suffix = uuid.uuid4().hex
    device_id = f"multi-device-{suffix}"
    with Session(engine) as session:
        user = User(name="multi-device-test", account=f"multi-{suffix}", hashed_password="x")
        session.add(user)
        session.commit()
        session.refresh(user)
        first = AssistantAIConfig(user_id=user.id, name=f"first-{suffix}")
        second = AssistantAIConfig(user_id=user.id, name=f"second-{suffix}")
        session.add(first)
        session.add(second)
        session.commit()
        session.refresh(first)
        session.refresh(second)
        user_id, first_id, second_id = user.id, first.id, second.id

    try:
        with Session(engine) as session:
            session.add(DeviceAiBinding(user_id=user_id, device_id=device_id, ai_config_id=first_id))
            session.add(DeviceAiBinding(user_id=user_id, device_id=device_id, ai_config_id=second_id))
            session.commit()
            rows = session.exec(
                select(DeviceAiBinding).where(
                    DeviceAiBinding.user_id == user_id,
                    DeviceAiBinding.device_id == device_id,
                )
            ).all()
            assert {row.ai_config_id for row in rows} == {first_id, second_id}

        set_scope(user_id, device_id, {"screen.read"}, ai_config_id=first_id)
        set_scope(user_id, device_id, {"touch.tap"}, ai_config_id=second_id)
        assert get_scope(user_id, device_id, first_id) == {"screen.read"}
        assert get_scope(user_id, device_id, second_id) == {"touch.tap"}

        with pytest.raises(IntegrityError):
            with Session(engine) as session:
                session.add(DeviceAiBinding(user_id=user_id, device_id=device_id, ai_config_id=first_id))
                session.commit()
    finally:
        with Session(engine) as session:
            for row in session.exec(
                select(DeviceTypeMcpPermission).where(
                    DeviceTypeMcpPermission.user_id == user_id,
                    DeviceTypeMcpPermission.device_id == device_id,
                )
            ).all():
                session.delete(row)
            for row in session.exec(
                select(DeviceAiBinding).where(
                    DeviceAiBinding.user_id == user_id,
                    DeviceAiBinding.device_id == device_id,
                )
            ).all():
                session.delete(row)
            for config_id in (first_id, second_id):
                row = session.get(AssistantAIConfig, config_id)
                if row:
                    session.delete(row)
            row = session.get(User, user_id)
            if row:
                session.delete(row)
            session.commit()


def test_postgres_persists_world_device_order_per_user():
    suffix = uuid.uuid4().hex
    with Session(engine) as session:
        user = User(name="world-order-test", account=f"world-order-{suffix}", hashed_password="x")
        session.add(user)
        session.commit()
        session.refresh(user)
        user_id = user.id

    try:
        with Session(engine) as session:
            save_world_device_order(session, user_id, [f"phone-{suffix}", f"desktop-{suffix}"])
            save_world_device_order(session, user_id, [f"desktop-{suffix}", f"phone-{suffix}"])
            assert list_world_device_order(session, user_id) == [
                f"desktop-{suffix}",
                f"phone-{suffix}",
            ]
    finally:
        with Session(engine) as session:
            for row in session.exec(
                select(WorldDeviceMeta).where(WorldDeviceMeta.user_id == user_id)
            ).all():
                session.delete(row)
            row = session.get(User, user_id)
            if row:
                session.delete(row)
            session.commit()
