import asyncio
import pytest
from sqlalchemy.pool import StaticPool
from sqlmodel import Session, SQLModel, create_engine, select

from api.models import (
    AssistantAIConfig,
    DeviceAiBinding,
    DevicePresence,
    User,
)
from api.models.maintenance import MaintenanceApproval, MaintenanceEvent, MaintenanceTask
from api.services.maintenance.service import (
    ApprovalRequestRecord, CreateTaskSpec, DeviceEventRecord,
    EventRecord, MaintenanceConflict, MaintenanceService, sanitize_payload,
)
from api.services.maintenance.state import validate_phase_transition, validate_status_transition
from api.services.maintenance.views import event_dto, task_dto
from api.services.maintenance.views import run_start_payload
from connector_runtime.maintenance import (
    ApprovalRequestedPayload,
    CommandAckPayload,
    DeviceEventPayload,
    pending_commands,
)
from api.sio import agents


@pytest.fixture()
def db():
    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool,
    )
    SQLModel.metadata.create_all(engine, tables=[
        User.__table__, AssistantAIConfig.__table__, DevicePresence.__table__,
        DeviceAiBinding.__table__, MaintenanceTask.__table__, MaintenanceEvent.__table__,
        MaintenanceApproval.__table__,
    ])
    with Session(engine) as session:
        user = User(name="owner", account="owner", hashed_password="x")
        session.add(user)
        session.commit()
        session.refresh(user)
        ai = AssistantAIConfig(user_id=user.id, name="Texas")
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
        yield session, int(user.id), int(ai.id)


def _create(db, *, dedupe_key="bug-1"):
    session, user_id, ai_id = db
    return MaintenanceService(session).create_task(user_id, CreateTaskSpec(
        maintainer_ai_config_id=ai_id, device_id="codex-local",
        title="Fix regression", description="Reproduce and fix", acceptance_criteria="tests pass",
        affected_repo="deploy/server", dedupe_key=dedupe_key,
    ))


def test_state_machine_rejects_terminal_revival_and_phase_regression():
    validate_status_transition("queued", "running")
    validate_status_transition("running", "waiting_user")
    validate_status_transition("waiting_user", "running")
    with pytest.raises(ValueError):
        validate_status_transition("succeeded", "running")
    with pytest.raises(ValueError):
        validate_phase_transition("test", "implement")


def test_create_is_deduplicated_and_requires_bound_codex_device(db):
    first = _create(db)
    second = _create(db)
    assert first.task_id == second.task_id
    service = MaintenanceService(db[0])
    with pytest.raises(MaintenanceConflict):
        service.create_task(db[1], CreateTaskSpec(
            maintainer_ai_config_id=db[2], device_id="missing",
            title="x", description="y",
        ))


def test_device_events_are_idempotent_monotonic_and_terminal(db):
    task = _create(db, dedupe_key="event-test")
    service = MaintenanceService(db[0])
    task, started = service.device_event(db[1], DeviceEventRecord(
        device_id="codex-local", run_id=task.run_id,
        event_id="device-2", sequence=1, event_type="run.started",
        payload={"summary": "started", "branch": "codex/maintenance/test", "baseSha": "abc123",
                 "workspace": "D:/private/worktree"}, status="running", phase="diagnose",
    ))
    _, duplicate = service.device_event(db[1], DeviceEventRecord(
        device_id="codex-local", run_id=task.run_id,
        event_id="device-2", sequence=1, event_type="run.started",
        payload={"summary": "ignored"}, status="running", phase="diagnose",
    ))
    assert duplicate.id == started.id
    assert task.last_sequence == 2
    assert task.last_device_sequence == 1
    assert task.branch_name == "codex/maintenance/test"
    assert task.base_sha == "abc123"
    public = task_dto(task)
    assert public["branch_name"] == "codex/maintenance/test"
    assert "workspace" not in public
    with pytest.raises(MaintenanceConflict):
        service.device_event(db[1], DeviceEventRecord(
            device_id="codex-local", run_id=task.run_id,
            event_id="device-4", sequence=3, event_type="run.event", payload={},
        ))
    service.device_event(db[1], DeviceEventRecord(
        device_id="codex-local", run_id=task.run_id,
        event_id="device-3", sequence=2, event_type="run.completed",
        payload={"summary": "done"}, status="succeeded", phase="verify",
    ))
    with pytest.raises(MaintenanceConflict):
        service.apply_state(task, status="running")


def test_approval_round_trip_and_public_dto(db):
    task = _create(db, dedupe_key="approval-test")
    service = MaintenanceService(db[0])
    service.device_event(db[1], DeviceEventRecord(
        device_id="codex-local", run_id=task.run_id,
        event_id="started", sequence=1, event_type="run.started",
        payload={}, status="running", phase="plan",
    ))
    approval = service.request_approval(task, ApprovalRequestRecord(
        event_id="approval-event", sequence=2, approval_id="approval-1",
        approval_type="command", title="Run migration", detail={"description": "Review it"},
        expires_at=None,
    ))
    task, approval, event = service.decide_approval(db[1], approval.approval_id, "approved")
    assert task.status == "running"
    assert approval.decision == "approved"
    assert task_dto(task)["id"] == task.task_id
    assert event_dto(event)["kind"] == "command.approval_decision"


def test_event_sanitizer_redacts_nested_credentials():
    safe = sanitize_payload({
        "authorization": "Bearer abcdefghijklmnop",
        "nested": {"api_token": "secret", "message": "Bearer abcdefghijklmnop"},
    })
    assert safe["authorization"] == "[redacted]"
    assert safe["nested"]["api_token"] == "[redacted]"
    assert safe["nested"]["message"] == "Bearer [redacted]"


def test_event_unique_contract_uses_run_and_event_id(db):
    task = _create(db, dedupe_key="unique-test")
    rows = db[0].exec(select(MaintenanceEvent).where(MaintenanceEvent.run_id == task.run_id)).all()
    assert [(row.sequence, row.event_type) for row in rows] == [(1, "task.created")]


def test_device_contract_preserves_app_server_event_and_thread_ids(db):
    parsed = DeviceEventPayload.model_validate({
        "runId": "run-1", "eventId": "event-1", "sequence": 1,
        "type": "item/agentMessage/delta", "data": {"delta": "hello"},
        "threadId": "thread-1", "turnId": "turn-1",
    })
    assert parsed.event_payload() == {
        "type": "item/agentMessage/delta", "data": {"delta": "hello"},
        "thread_id": "thread-1", "turn_id": "turn-1",
    }
    task = _create(db, dedupe_key="prompt-contract")
    start = run_start_payload(task)
    assert task.title in start["prompt"]
    assert start["approvalPolicy"] == "unlessTrusted"
    assert start["sandboxPolicy"]["type"] == "workspaceWrite"


def test_device_contract_accepts_app_server_approval_and_command_ack():
    approval = ApprovalRequestedPayload.model_validate({
        "runId": "run-1", "eventId": "event-1", "sequence": 1,
        "approvalId": "approval-1", "method": "item/commandExecution/requestApproval",
        "request": {"command": "pytest"},
    })
    assert approval.method.endswith("requestApproval")
    assert approval.request == {"command": "pytest"}
    ack = CommandAckPayload.model_validate({
        "commandId": "command-1", "command": "steer", "success": True,
        "eventId": "ack-1",
    })
    assert ack.command_id == "command-1"


@pytest.mark.parametrize(
    ("kind", "delta"),
    [
        ("item/commandExecution/outputDelta", "pytest output"),
        ("item/reasoning/summaryTextDelta", "checking the failure"),
        ("turn/diff/updated", "2 files changed"),
    ],
)
def test_app_server_event_types_remain_distinguishable_in_web_dto(db, kind, delta):
    task = _create(db, dedupe_key=f"event-kind-{kind}")
    service = MaintenanceService(db[0])
    row = service.append_event(task, EventRecord(
        kind, "device", {"type": kind, "data": {"delta": delta}},
    ))
    db[0].commit()
    public = event_dto(row)
    assert public["kind"] == kind
    assert public["summary"] == delta


def test_offline_commands_are_durable_and_reconnect_replays_only_unacked(db, monkeypatch):
    task = _create(db, dedupe_key="offline-replay")
    service = MaintenanceService(db[0])
    service.append_event(task, EventRecord(
        "dispatch.waiting_device", "system",
        {"command_id": f"run_start:{task.run_id}", "command": "run_start"},
        event_id=f"waiting:run_start:{task.run_id}",
    ))
    db[0].commit()
    service.device_event(db[1], DeviceEventRecord(
        device_id="codex-local", run_id=task.run_id, event_id="started-offline",
        sequence=1, event_type="run.started", payload={}, status="running",
    ))
    service.append_event(task, EventRecord(
        "command.steer", "user",
        {"command_id": "steer-1", "command": "steer", "text": "focus tests"},
        event_id="cmd:steer-1",
    ))
    service.append_event(task, EventRecord(
        "dispatch.waiting_device", "system",
        {"command_id": "steer-1", "command": "steer"}, event_id="waiting:steer-1",
    ))
    db[0].commit()
    approval = service.request_approval(task, ApprovalRequestRecord(
        event_id="approval-offline", sequence=2, approval_id="approval-offline",
        approval_type="command", title="command", detail={}, expires_at=None,
    ))
    task, approval, _ = service.decide_approval(
        db[1], approval.approval_id, "denied", "use another approach", "approval-command-1",
    )
    assert task.status == "running"
    service.append_event(task, EventRecord(
        "dispatch.waiting_device", "system",
        {"command_id": "approval-command-1", "command": "approval_decision"},
        event_id="waiting:approval-command-1",
    ))
    service.append_event(task, EventRecord(
        "command.acknowledged", "device", {"command_id": "steer-1", "success": True},
        event_id="ack:steer-1",
    ))
    db[0].commit()
    rows = db[0].exec(select(MaintenanceEvent).where(
        MaintenanceEvent.task_id == task.task_id,
    )).all()
    outstanding = pending_commands(rows)
    assert [event for event, _ in outstanding] == ["codex:approval_decision"]
    assert outstanding[0][1]["decision"] == "decline"

    import connector_runtime.maintenance as connector_maintenance

    emitted = []

    async def fake_emit(event, payload, to=None, **_kwargs):
        emitted.append((event, payload, to))

    monkeypatch.setattr(connector_maintenance, "engine", db[0].get_bind())
    monkeypatch.setattr(connector_maintenance.sio, "emit", fake_emit)
    agents["codex-sid"] = {
        "id": "codex-local", "userId": db[1], "platform": "codex-maintainer",
        "boundAiConfigIds": [db[2]],
    }
    try:
        asyncio.run(connector_maintenance.resume_codex_maintenance(
            "codex-local", db[1], (db[2],),
        ))
    finally:
        agents.pop("codex-sid", None)
    names = [event for event, _payload, _sid in emitted]
    assert names == ["codex:run_start", "codex:approval_decision"]
    assert emitted[0][1]["commandId"] == f"run_start:{task.run_id}"

    emitted.clear()
    task.status = "succeeded"
    db[0].add(task)
    db[0].commit()
    asyncio.run(connector_maintenance.resume_codex_maintenance(
        "codex-local", db[1], (db[2],),
    ))
    assert emitted == []


def test_offline_rest_mutations_return_waiting_instead_of_false_failure(db, monkeypatch):
    import gateway.routers.maintenance as routes

    user = db[0].get(User, db[1])

    async def offline(*_args, **_kwargs):
        return False

    async def no_emit(*_args, **_kwargs):
        return None

    monkeypatch.setattr(routes, "get_current_user", lambda *_args: user)
    monkeypatch.setattr(routes, "_send_device_command", offline)
    monkeypatch.setattr(routes, "_emit_update", no_emit)
    created = asyncio.run(routes.create_task(
        routes.CreateTaskRequest(
            maintainer_ai_config_id=db[2], device_id="codex-local", title="offline",
            description="offline create", acceptance_criteria="accepted",
            affected_repo="deploy/server", dedupe_key="offline-rest",
        ),
        session=db[0], authorization="Bearer ignored",
    ))
    assert created["delivery_status"] == "waiting_device"
    task_id = created["id"]
    service = MaintenanceService(db[0])
    task = service.owned_task(db[1], task_id)
    start_command = db[0].exec(select(MaintenanceEvent).where(
        MaintenanceEvent.task_id == task_id,
        MaintenanceEvent.event_id == f"cmd:run_start:{task.run_id}",
    )).first()
    assert start_command is not None
    start_payload = MaintenanceService.event_payload(start_command)["payload"]
    assert start_payload["command_id"] == f"run_start:{task.run_id}"
    assert start_payload["prompt"].startswith("维护工单")
    service.device_event(db[1], DeviceEventRecord(
        device_id="codex-local", run_id=task.run_id, event_id="rest-started",
        sequence=1, event_type="run.started", payload={}, status="running",
    ))
    steered = asyncio.run(routes.steer(
        task_id, routes.CommandRequest(content="keep it small", request_id="rest-steer"),
        session=db[0], authorization="Bearer ignored",
    ))
    assert steered["accepted"] is True
    assert steered["delivery_status"] == "waiting_device"
    waiting = db[0].exec(select(MaintenanceEvent).where(
        MaintenanceEvent.task_id == task_id,
        MaintenanceEvent.event_id == "waiting:rest-steer",
    )).first()
    assert waiting is not None
    task = service.owned_task(db[1], task_id)
    approval = service.request_approval(task, ApprovalRequestRecord(
        event_id="rest-approval-event", sequence=2, approval_id="rest-approval",
        approval_type="command", title="run command", detail={}, expires_at=None,
    ))
    decided = asyncio.run(routes.decide(
        approval.approval_id,
        routes.ApprovalDecisionRequest(decision="denied", comment="find another way"),
        session=db[0], authorization="Bearer ignored",
    ))
    assert decided["accepted"] is True
    assert decided["delivery_status"] == "waiting_device"
    assert decided["task"]["status"] == "running"
    command = db[0].exec(select(MaintenanceEvent).where(
        MaintenanceEvent.task_id == task_id,
        MaintenanceEvent.event_id == "cmd:approval:rest-approval",
    )).one()
    assert MaintenanceService.event_payload(command)["payload"]["decision"] == "decline"
