import json

from sqlmodel import Session, select

from api.models import WorkflowRun, WorkflowStepRun
from api.services.workflows.run_service import RunActorContext, advance_run, apply_step_result, create_run
from tools.automation import _resume_run
from test_workflow_run_service import _database, _seed


def test_debug_run_can_start_at_any_step_and_pause_after_one_transition(monkeypatch):
    definition = {
        "schemaVersion": 1,
        "inputSchema": {"type": "object"},
        "startStepId": "first",
        "limits": {"timeoutSeconds": 60, "maxTransitions": 6},
        "steps": {
            "first": {"type": "delay", "delaySeconds": 0, "next": "second"},
            "second": {
                "type": "condition", "expression": {"op": "eq", "left": 1, "right": 1},
                "onTrue": "finish", "onFalse": "finish",
            },
            "finish": {"type": "end"},
        },
        "output": {},
    }
    engine = _database()
    with Session(engine) as session:
        user, card = _seed(session, definition)
        run = create_run(
            session, user_id=user.id, card_id=card.id, device_id="device", input_value={},
            actor=RunActorContext(initial_variables={
                "_debug": {"pause_after_step": False},
                "_run_debug_options": {"start_step_id": "second", "start_paused": True},
            }),
        )
        assert run.status == "paused"
        assert run.current_step_id == "second"
        run_id = run.id
        user_id = user.id

    monkeypatch.setattr("tools.automation.engine", engine)
    resumed = _resume_run(user_id, {"run_id": run_id, "_debug_single_step": True}, None)
    assert resumed["status"] == "pending"

    with Session(engine) as session:
        advance_run(session, run_id)
        row = session.get(WorkflowRun, run_id)
        assert row.status == "paused"
        assert row.current_step_id == "finish"
        assert json.loads(row.variables_json)["_debug"]["last_completed_step_id"] == "second"


def test_debug_step_pauses_after_a_completed_device_call(monkeypatch):
    definition = {
        "schemaVersion": 1,
        "inputSchema": {"type": "object"},
        "startStepId": "call",
        "limits": {"timeoutSeconds": 60, "maxTransitions": 4},
        "steps": {
            "call": {
                "type": "mcp", "toolRef": {"namespace": "device", "name": "demo"},
                "arguments": {}, "saveAs": "demo", "next": "finish",
            },
            "finish": {"type": "end"},
        },
        "output": {},
    }
    engine = _database()
    with Session(engine) as session:
        user, card = _seed(session, definition)
        run = create_run(
            session, user_id=user.id, card_id=card.id, device_id="device", input_value={},
            actor=RunActorContext(initial_variables={
                "_debug": {"pause_after_step": False},
                "_run_debug_options": {"start_paused": True},
            }),
        )
        run_id, user_id = run.id, user.id
    monkeypatch.setattr("tools.automation.engine", engine)
    _resume_run(user_id, {"run_id": run_id, "_debug_single_step": True}, None)

    with Session(engine) as session:
        advance_run(session, run_id)
        step = session.exec(select(WorkflowStepRun).where(WorkflowStepRun.run_id == run_id)).one()
        assert apply_step_result(
            session, dispatch_task_id=step.dispatch_task_id, success=True, result={"ok": True},
        )
        row = session.get(WorkflowRun, run_id)
        assert row.status == "paused"
        assert row.current_step_id == "finish"


def test_debug_failure_does_not_resurrect_a_terminal_run(monkeypatch):
    definition = {
        "schemaVersion": 1, "inputSchema": {"type": "object"}, "startStepId": "call",
        "limits": {"timeoutSeconds": 60, "maxTransitions": 3},
        "steps": {
            "call": {
                "type": "mcp", "toolRef": {"namespace": "device", "name": "demo"},
                "arguments": {}, "saveAs": "demo", "next": "finish", "onError": "fail",
            },
            "finish": {"type": "end"},
        },
        "output": {},
    }
    engine = _database()
    with Session(engine) as session:
        user, card = _seed(session, definition)
        run = create_run(
            session, user_id=user.id, card_id=card.id, device_id="device", input_value={},
            actor=RunActorContext(initial_variables={
                "_debug": {"pause_after_step": False},
                "_run_debug_options": {"start_paused": True},
            }),
        )
        run_id, user_id = run.id, user.id
    monkeypatch.setattr("tools.automation.engine", engine)
    _resume_run(user_id, {"run_id": run_id, "_debug_single_step": True}, None)

    with Session(engine) as session:
        advance_run(session, run_id)
        step = session.exec(select(WorkflowStepRun).where(WorkflowStepRun.run_id == run_id)).one()
        apply_step_result(session, dispatch_task_id=step.dispatch_task_id, success=False, error="boom")
        assert session.get(WorkflowRun, run_id).status == "failed"


def test_debug_environment_preparation_pauses_at_requested_target():
    definition = {
        "schemaVersion": 1,
        "inputSchema": {"type": "object"},
        "startStepId": "reset",
        "limits": {"timeoutSeconds": 60, "maxTransitions": 8},
        "compatibility": {"initialEnvironment": {
            "description": "reload and wait", "resetStepId": "reset", "readyStepId": "ready",
        }},
        "steps": {
            "reset": {"type": "delay", "delaySeconds": 0, "next": "ready"},
            "ready": {"type": "condition", "expression": {"op": "eq", "left": 1, "right": 1},
                      "onTrue": "normal", "onFalse": "normal"},
            "normal": {"type": "delay", "delaySeconds": 0, "next": "target"},
            "target": {"type": "end"},
        },
        "output": {},
    }
    engine = _database()
    with Session(engine) as session:
        user, card = _seed(session, definition)
        run = create_run(
            session, user_id=user.id, card_id=card.id, device_id="device", input_value={},
            actor=RunActorContext(initial_variables={
                "_debug": {"pause_after_step": False, "prepare_ready_step_id": "ready",
                           "prepare_target_step_id": "target"},
                "_run_debug_options": {"start_step_id": "reset", "start_paused": False},
            }),
        )
        advance_run(session, run.id)
        advance_run(session, run.id)
        row = session.get(WorkflowRun, run.id)
        assert row.status == "paused"
        assert row.current_step_id == "target"
