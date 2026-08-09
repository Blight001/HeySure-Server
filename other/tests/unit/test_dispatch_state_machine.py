import asyncio
import time
from unittest.mock import AsyncMock

import pytest
from sqlalchemy.pool import StaticPool
from sqlmodel import SQLModel, Session, create_engine, select

from api.models import AgentDispatchTask
from connector_runtime.dispatch import device_dispatch
from connector_runtime.dispatch import repository
from connector_runtime.dispatch.models import can_transition, require_transition


def _database():
    db = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    SQLModel.metadata.create_all(db, tables=[AgentDispatchTask.__table__])
    return db


def test_transition_table_rejects_terminal_resurrection():
    assert can_transition("queued", "pending")
    assert can_transition("pending", "completed")
    assert not can_transition("timeout", "completed")
    with pytest.raises(ValueError, match="illegal dispatch transition"):
        require_transition("cancelled", "pending")


def test_previous_connector_owner_is_reaped_immediately(monkeypatch):
    db = _database()
    monkeypatch.setattr(repository, "engine", db)
    with Session(db) as session:
        session.add(
            AgentDispatchTask(
                task_id="task-old-owner",
                user_id=1,
                device_id="device-1",
                status="pending",
                owner_instance_id="connector-old",
                lease_expires_at=time.time() + 300,
            )
        )
        session.commit()

    assert device_dispatch.expire_orphan_dispatches(older_than_seconds=3600) == 1

    with Session(db) as session:
        row = session.exec(
            select(AgentDispatchTask).where(AgentDispatchTask.task_id == "task-old-owner")
        ).one()
        assert row.status == "timeout"
        assert "previous connector-runtime" in (row.error or "")
        assert row.lease_expires_at is None


def test_late_result_cannot_overwrite_timeout(monkeypatch):
    db = _database()
    monkeypatch.setattr(repository, "engine", db)
    with Session(db) as session:
        session.add(
            AgentDispatchTask(
                task_id="task-timeout",
                user_id=1,
                device_id="device-1",
                status="timeout",
                error="deadline reached",
            )
        )
        session.commit()

    device_dispatch._finalize_dispatch_row(
        "task-timeout", status="completed", success=True, result={"late": True}
    )

    with Session(db) as session:
        row = session.exec(
            select(AgentDispatchTask).where(AgentDispatchTask.task_id == "task-timeout")
        ).one()
        assert row.status == "timeout"
        assert row.result_json is None


def test_resume_promotes_oldest_queued_task(monkeypatch):
    db = _database()
    monkeypatch.setattr(repository, "engine", db)
    monkeypatch.setattr(device_dispatch, "engine", db)
    monkeypatch.setattr(device_dispatch, "_PENDING_DISPATCHES", {})
    monkeypatch.setitem(device_dispatch.agents, "sid-device", {"id": "device-1"})
    monkeypatch.setattr(device_dispatch.sio, "emit", AsyncMock())
    with Session(db) as session:
        session.add(
            AgentDispatchTask(
                task_id="queued-first", user_id=1, device_id="device-1",
                status="queued", created_at=1,
            )
        )
        session.add(
            AgentDispatchTask(
                task_id="queued-second", user_id=1, device_id="device-1",
                status="queued", created_at=2,
            )
        )
        session.commit()

    assert asyncio.run(device_dispatch.resume_device_dispatch_queue("device-1")) == "queued-first"

    with Session(db) as session:
        first = session.exec(
            select(AgentDispatchTask).where(AgentDispatchTask.task_id == "queued-first")
        ).one()
        second = session.exec(
            select(AgentDispatchTask).where(AgentDispatchTask.task_id == "queued-second")
        ).one()
        assert first.status == "pending"
        assert first.owner_instance_id == repository.CONNECTOR_INSTANCE_ID
        assert first.attempt == 1
        assert second.status == "queued"


def test_expire_releases_queue_and_promotes_next_task(monkeypatch):
    db = _database()
    monkeypatch.setattr(repository, "engine", db)
    monkeypatch.setattr(device_dispatch, "engine", db)
    monkeypatch.setattr(device_dispatch, "_PENDING_DISPATCHES", {})
    monkeypatch.setitem(device_dispatch.agents, "sid-device", {"id": "device-1"})
    monkeypatch.setattr(device_dispatch.sio, "emit", AsyncMock())
    with Session(db) as session:
        session.add(
            AgentDispatchTask(
                task_id="pending", user_id=1, device_id="device-1", status="pending"
            )
        )
        session.add(
            AgentDispatchTask(
                task_id="next", user_id=1, device_id="device-1", status="queued"
            )
        )
        session.commit()

    assert asyncio.run(device_dispatch.expire_dispatch("pending", "caller timeout"))

    with Session(db) as session:
        expired = session.exec(
            select(AgentDispatchTask).where(AgentDispatchTask.task_id == "pending")
        ).one()
        promoted = session.exec(
            select(AgentDispatchTask).where(AgentDispatchTask.task_id == "next")
        ).one()
        assert expired.status == "timeout"
        assert expired.error == "caller timeout"
        assert promoted.status == "pending"
