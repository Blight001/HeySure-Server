import json
import os

os.environ.setdefault("DATABASE_URL", "postgresql+psycopg://test:test@127.0.0.1/test")
os.environ.setdefault("HEYSURE_INTERNAL_TOKEN", "test")

from sqlmodel import Session, SQLModel, create_engine, select

from api.models import (
    AgentDispatchTask,
    DevicePresence,
    User,
    WorkflowAuditEvent,
    WorkflowCard,
    WorkflowCardVersion,
    WorkflowConfirmation,
    WorkflowRun,
    WorkflowStepRun,
)
from api.services.workflows.compiler import definition_digest
from api.services.workflows.compiler import schema_digest
from api.services.workflows.permissions import WorkflowDispatchError, validate_step_dispatch
from api.services.workflows.run_service import (
    advance_run,
    apply_step_result,
    cancel_run,
    create_run,
    decide_confirmation,
)


def _database():
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False})
    tables = [
        User.__table__, DevicePresence.__table__, WorkflowCard.__table__, WorkflowCardVersion.__table__,
        WorkflowRun.__table__, WorkflowStepRun.__table__, WorkflowConfirmation.__table__,
        WorkflowAuditEvent.__table__,
        AgentDispatchTask.__table__,
    ]
    SQLModel.metadata.create_all(engine, tables=tables)
    return engine


def _seed(session: Session, definition: dict, *, tool_contracts=None):
    user = User(name="Test", account="test", hashed_password="x")
    session.add(user)
    session.commit()
    session.refresh(user)
    card = WorkflowCard(
        id="card", user_id=user.id, created_by=user.id, name="Test", status="published",
        draft_definition_json=json.dumps(definition),
    )
    version = WorkflowCardVersion(
        id="version", card_id=card.id, version_number=1, definition_json=json.dumps(definition),
        definition_digest=definition_digest(definition), tool_contracts_json=json.dumps(tool_contracts or {}),
        published_by=user.id,
    )
    session.add(card)
    session.add(version)
    session.flush()
    card.latest_version_id = version.id
    session.add(card)
    session.add(DevicePresence(user_id=user.id, device_id="device", device_type="desktop", online=True))
    session.commit()
    return user, card


def test_condition_delay_confirmation_and_end_are_deterministic():
    definition = {
        "schemaVersion": 1,
        "inputSchema": {"type": "object", "properties": {"approved": {"type": "boolean"}}, "required": ["approved"]},
        "startStepId": "choose",
        "limits": {"timeoutSeconds": 60, "maxTransitions": 10},
        "steps": {
            "choose": {"type": "condition", "expression": {"op": "eq", "left": "${input.approved}", "right": True}, "onTrue": "delay", "onFalse": "finish"},
            "delay": {"type": "delay", "delaySeconds": 0, "next": "confirm"},
            "confirm": {"type": "confirm", "message": "continue", "next": "finish"},
            "finish": {"type": "end"},
        },
        "output": {"ok": True},
    }
    engine = _database()
    with Session(engine) as session:
        user, card = _seed(session, definition)
        run = create_run(session, user_id=user.id, card_id=card.id, device_id="device", input_value={"approved": True})
        assert "approved" not in run.input_json
        advance_run(session, run.id)
        advance_run(session, run.id)
        advance_run(session, run.id)
        session.refresh(run)
        assert run.status == "waiting_confirmation"
        decide_confirmation(session, run=run, user_id=user.id, approved=True)
        advance_run(session, run.id)
        session.refresh(run)
        assert run.status == "succeeded"
        assert json.loads(run.output_json) == {"ok": True}


def test_failed_mcp_attempt_retries_once_and_duplicate_result_does_not_advance():
    definition = {
        "schemaVersion": 1,
        "inputSchema": {"type": "object"},
        "startStepId": "call",
        "limits": {"timeoutSeconds": 60, "maxTransitions": 10},
        "steps": {
            "call": {
                "type": "mcp", "toolRef": {"namespace": "device", "name": "demo", "schemaDigest": "sha256:x"},
                "arguments": {}, "saveAs": "demo", "timeoutSeconds": 10,
                "retryPolicy": {"maxAttempts": 2, "delaySeconds": 0, "retryOn": ["DISPATCH_FAILED"]},
                "next": "finish",
            },
            "finish": {"type": "end"},
        },
        "output": {"value": "${steps.demo.result.value}"},
    }
    engine = _database()
    with Session(engine) as session:
        user, card = _seed(session, definition, tool_contracts={"demo": {"destructive": False}})
        run = create_run(session, user_id=user.id, card_id=card.id, device_id="device", input_value={})
        advance_run(session, run.id)
        first = session.exec(select(WorkflowStepRun).where(WorkflowStepRun.run_id == run.id)).one()
        assert apply_step_result(session, dispatch_task_id=first.dispatch_task_id, success=False, error="temporary")
        session.refresh(run)
        assert run.status == "retry_wait"
        advance_run(session, run.id)
        attempts = session.exec(
            select(WorkflowStepRun).where(WorkflowStepRun.run_id == run.id).order_by(WorkflowStepRun.attempt)
        ).all()
        assert [item.attempt for item in attempts] == [1, 2]
        second = attempts[-1]
        assert apply_step_result(session, dispatch_task_id=second.dispatch_task_id, success=True, result={"value": 7})
        assert not apply_step_result(session, dispatch_task_id=second.dispatch_task_id, success=True, result={"value": 9})
        advance_run(session, run.id)
        session.refresh(run)
        assert json.loads(run.output_json) == {"value": 7}


def test_dispatch_rechecks_scope_schema_arguments_and_confirmation(monkeypatch):
    definition = {"schemaVersion": 1, "inputSchema": {"type": "object"}, "startStepId": "finish", "steps": {"finish": {"type": "end"}}, "limits": {"timeoutSeconds": 60, "maxTransitions": 2}, "output": {}}
    engine = _database()
    tool_schema = {
        "type": "object",
        "properties": {"name": {"type": "string"}},
        "required": ["name"],
        "additionalProperties": False,
    }
    with Session(engine) as session:
        user, card = _seed(session, definition)
        device = session.exec(select(DevicePresence).where(DevicePresence.device_id == "device")).one()
        device.tool_defs_json = json.dumps({
            "demo": {"input_schema": tool_schema, "destructive": True, "permissions": ["filesystem"]}
        })
        session.add(device)
        session.commit()
        monkeypatch.setattr("api.services.workflows.permissions.get_scope", lambda *_: {"demo"})
        monkeypatch.setattr("api.services.workflows.permissions.get_policy", lambda *_: {"filesystem": "allow"})
        validated = validate_step_dispatch(
            session,
            user_id=user.id,
            device_id="device",
            tool_name="demo",
            expected_provider="desktop",
            expected_digest=schema_digest(tool_schema),
            arguments={"name": "ok"},
            confirmation_granted=True,
            card_id=card.id,
            card_version_id="version",
        )
        assert validated.device_id == "device"
        try:
            validate_step_dispatch(
                session, user_id=user.id, device_id="device", tool_name="demo",
                expected_provider="desktop", expected_digest="sha256:changed", arguments={"name": "ok"},
                confirmation_granted=True, card_id=card.id, card_version_id="version",
            )
        except WorkflowDispatchError as exc:
            assert exc.code == "TOOL_SCHEMA_INCOMPATIBLE"
        else:
            raise AssertionError("schema drift must be rejected")
        try:
            validate_step_dispatch(
                session, user_id=user.id, device_id="device", tool_name="demo",
                expected_provider="desktop", expected_digest=schema_digest(tool_schema), arguments={"name": "ok"},
                confirmation_granted=False, card_id=card.id, card_version_id="version",
            )
        except WorkflowDispatchError as exc:
            assert exc.code == "CONFIRMATION_REQUIRED"
        else:
            raise AssertionError("destructive tool must require confirmation")


def test_cancel_run_terminalizes_pending_device_dispatch():
    definition = {
        "schemaVersion": 1,
        "inputSchema": {"type": "object"},
        "startStepId": "call",
        "limits": {"timeoutSeconds": 60, "maxTransitions": 3},
        "steps": {
            "call": {
                "type": "mcp",
                "toolRef": {"namespace": "device", "name": "demo", "schemaDigest": "sha256:x"},
                "arguments": {},
                "timeoutSeconds": 30,
                "next": "finish",
            },
            "finish": {"type": "end"},
        },
        "output": {},
    }
    db = _database()
    with Session(db) as session:
        user, card = _seed(session, definition, tool_contracts={"demo": {}})
        run = create_run(
            session,
            user_id=user.id,
            card_id=card.id,
            device_id="device",
            input_value={},
        )
        advance_run(session, run.id)
        step = session.exec(
            select(WorkflowStepRun).where(WorkflowStepRun.run_id == run.id)
        ).one()
        session.add(AgentDispatchTask(
            task_id=step.dispatch_task_id,
            user_id=user.id,
            device_id="device",
            status="pending",
            lease_expires_at=9999999999,
        ))
        session.commit()

        cancel_run(session, run, "user stopped workflow")

        session.refresh(step)
        dispatch = session.exec(
            select(AgentDispatchTask).where(
                AgentDispatchTask.task_id == step.dispatch_task_id
            )
        ).one()
        assert run.status == "cancelled"
        assert step.status == "cancelled"
        assert dispatch.status == "cancelled"
        assert dispatch.success is False
        assert dispatch.error == "user stopped workflow"
        assert dispatch.lease_expires_at is None
