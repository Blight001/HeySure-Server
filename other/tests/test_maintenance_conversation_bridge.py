import asyncio

import pytest
from sqlalchemy.pool import StaticPool
from sqlmodel import Session, SQLModel, create_engine, select

from api.models import (
    AssistantAIConfig, ChatMessage, ChatSession, DeviceAiBinding, DevicePresence,
    TokenUsageSnapshot, User,
)
from api.models.external_control import ExternalControllerTurn
from api.models.maintenance import MaintenanceApproval, MaintenanceEvent, MaintenanceTask
from api.services.external_control.service import ExternalControlService
from api.sio import agents
from connector_runtime import maintenance_conversation_bridge as bridge


@pytest.fixture()
def bridge_db(monkeypatch):
    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool,
    )
    SQLModel.metadata.create_all(engine, tables=[
        User.__table__, AssistantAIConfig.__table__, DevicePresence.__table__,
        DeviceAiBinding.__table__, ChatMessage.__table__, ChatSession.__table__,
        TokenUsageSnapshot.__table__, ExternalControllerTurn.__table__,
        MaintenanceTask.__table__, MaintenanceEvent.__table__, MaintenanceApproval.__table__,
    ])
    with Session(engine) as session:
        user = User(name="owner", account="owner", hashed_password="x")
        session.add(user)
        session.commit()
        session.refresh(user)
        ai = AssistantAIConfig(
            user_id=user.id, name="Texas", execution_mode="external_mcp",
        )
        session.add(ai)
        session.commit()
        session.refresh(ai)
        session.add(DevicePresence(
            user_id=user.id, device_id="codex-local", device_type="custom",
            platform="codex-maintainer", online=True,
        ))
        session.add(DeviceAiBinding(
            user_id=user.id, device_id="codex-local", ai_config_id=ai.id,
        ))
        session.commit()
        turn = ExternalControlService(session).enqueue_message(
            int(user.id), int(ai.id), content="Please inspect the project",
            session_id="chat-1", session_name="Direct chat", ai_kind="core",
        )
        turn_id = turn.turn_id
        user_id = int(user.id)
        ai_id = int(ai.id)
    monkeypatch.setattr(bridge, "engine", engine)
    yield engine, user_id, ai_id, turn_id


def test_queued_external_message_dispatches_and_replies_to_original_chat(
    bridge_db, monkeypatch,
):
    engine, user_id, ai_id, turn_id = bridge_db
    emitted = []

    async def fake_emit(event, payload, to=None, **_kwargs):
        emitted.append((event, payload, to))

    monkeypatch.setattr(bridge.sio, "emit", fake_emit)
    agents["codex-sid"] = {
        "id": "codex-local", "userId": user_id, "platform": "codex-maintainer",
        "boundAiConfigIds": [ai_id], "dispatchable": True,
    }
    try:
        assert asyncio.run(bridge.dispatch_queued_turns()) == 1
    finally:
        agents.pop("codex-sid", None)

    assert emitted[0][0] == "codex:run_start"
    assert emitted[0][1]["commandId"].startswith("run_start:")
    assert emitted[0][1]["workspaceMode"] == "current"
    assert emitted[0][1]["sandboxPolicy"] == {"type": "dangerFullAccess"}
    assert emitted[0][1]["approvalPolicy"] == "never"
    assert "baota" in emitted[0][1]["trustedMcpServers"]
    with Session(engine) as session:
        turn = session.get(ExternalControllerTurn, turn_id)
        task = session.exec(select(MaintenanceTask).where(
            MaintenanceTask.dedupe_key == f"external_turn:{turn_id}",
        )).one()
        assert turn.status == "running"
        assert "Please inspect the project" in task.description
        assert "本机 Codex 控制器" in task.description
        task.status = "succeeded"
        task.summary = "Project inspection complete"
        session.add(task)
        session.commit()
        session.refresh(task)

    assert bridge.complete_conversation_task(task) is True
    assert bridge.complete_conversation_task(task) is True
    with Session(engine) as session:
        turn = session.get(ExternalControllerTurn, turn_id)
        reply = session.get(ChatMessage, turn.assistant_message_id)
        assert turn.status == "succeeded"
        assert reply.content == "Project inspection complete"
        assert reply.session_id == "chat-1"
