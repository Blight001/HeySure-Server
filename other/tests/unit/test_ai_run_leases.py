import time

from sqlalchemy.pool import StaticPool
from sqlmodel import SQLModel, Session, create_engine, select

from api.models import ChatRun
from api.runtime import heartbeat


def _database():
    db = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    SQLModel.metadata.create_all(db, tables=[ChatRun.__table__])
    return db


def _run(run_id: str, lease_expires_at: float) -> ChatRun:
    return ChatRun(
        run_id=run_id,
        user_id=1,
        status="running",
        worker_instance_id="worker-dead",
        lease_expires_at=lease_expires_at,
        heartbeat_at=time.time(),
    )


def test_reaper_uses_explicit_lease_not_recent_heartbeat(monkeypatch):
    db = _database()
    monkeypatch.setattr(heartbeat, "engine", db)
    with Session(db) as session:
        session.add(_run("expired", time.time() - 1))
        session.add(_run("live", time.time() + 60))
        session.commit()

    assert heartbeat.reap_stale_runs() == ["expired"]

    with Session(db) as session:
        expired = session.exec(select(ChatRun).where(ChatRun.run_id == "expired")).one()
        live = session.exec(select(ChatRun).where(ChatRun.run_id == "live")).one()
        assert expired.status == "queued"
        assert expired.worker_instance_id is None
        assert live.status == "running"
