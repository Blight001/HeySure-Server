import json
import os

import pytest

os.environ.setdefault("DATABASE_URL", "postgresql+psycopg://test:test@127.0.0.1/test")
os.environ.setdefault("HEYSURE_INTERNAL_TOKEN", "test")

from sqlmodel import Session, SQLModel, create_engine, select

from api.models import (
    AgentDispatchTask,
    AssistantAIConfig,
    ChatMessage,
    ChatRun,
    ChatSession,
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
from api.services.workflows.card_service import _snapshot_contracts
from api.services.workflows.permissions import WorkflowDispatchError, validate_step_dispatch
from api.services.workflows.run_service import (
    RUN_CONFIRMATION_SCOPE,
    advance_run,
    apply_step_result,
    cancel_run,
    confirmation_granted,
    create_run,
    decide_confirmation,
    expire_confirmations,
    request_confirmation,
    RunActorContext,
)
from api.services.workflows.expression import resolve_target_arguments
from api.services.workflows.step_device_binding import step_run_device_id
from api.services.workflows.ai_interaction import (
    AI_INTERVENTION_TOOL,
    advance_interactive_run,
    create_validated_run,
    expire_ai_interactions,
    respond_ai_interaction,
)
from api.services.workflows.ai_interaction_notifier import process_pending_ai_interactions
from api.services.workflows.confirmation_notifications import (
    notification_events_since,
    pending_notifications,
)
from tools.automation import _wait_for_original_chat_run


def _database():
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False})
    tables = [
        User.__table__, AssistantAIConfig.__table__, ChatSession.__table__, ChatMessage.__table__, ChatRun.__table__,
        DevicePresence.__table__, WorkflowCard.__table__, WorkflowCardVersion.__table__,
        WorkflowRun.__table__, WorkflowStepRun.__table__, WorkflowConfirmation.__table__,
        WorkflowAuditEvent.__table__,
        AgentDispatchTask.__table__,
    ]
    SQLModel.metadata.create_all(engine, tables=tables)
    return engine


def _seed(session: Session, definition: dict, *, tool_contracts=None, contract_device_ids=None):
    user = User(name="Test", account="test", hashed_password="x")
    session.add(user)
    session.commit()
    session.refresh(user)
    session.add(AssistantAIConfig(id=7, user_id=user.id, name="Device AI"))
    session.add(AssistantAIConfig(id=9, user_id=user.id, name="Running AI"))
    card = WorkflowCard(
        id="card", user_id=user.id, created_by=user.id, name="Test", status="published",
        draft_definition_json=json.dumps(definition),
    )
    version = WorkflowCardVersion(
        id="version", card_id=card.id, version_number=1, definition_json=json.dumps(definition),
        definition_digest=definition_digest(definition), tool_contracts_json=json.dumps(tool_contracts or {}),
        contract_device_ids_json=json.dumps(contract_device_ids or []),
        published_by=user.id,
    )
    session.add(card)
    session.add(version)
    session.flush()
    card.latest_version_id = version.id
    session.add(card)
    tool_defs = {
        str(step.get("toolRef", {}).get("name")): {"input_schema": {}}
        for step in definition.get("steps", {}).values()
        if isinstance(step, dict) and step.get("type") == "mcp"
    }
    session.add(DevicePresence(
        user_id=user.id, device_id="device", device_type="desktop", online=True,
        ai_config_id=7, tool_defs_json=json.dumps(tool_defs),
    ))
    session.commit()
    return user, card


def test_condition_delay_ai_mediated_confirmation_and_end_are_deterministic():
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
        advance_interactive_run(session, run.id)
        advance_interactive_run(session, run.id)
        advance_interactive_run(session, run.id)
        session.refresh(run)
        assert run.status == "waiting_confirmation"
        decide_confirmation(session, run=run, user_id=user.id, approved=True)
        advance_run(session, run.id)
        session.refresh(run)
        assert run.status == "succeeded"
        assert json.loads(run.output_json) == {"ok": True}


def test_run_scoped_confirmation_renews_dispatch_deadline_and_covers_later_steps():
    definition = {
        "schemaVersion": 1,
        "inputSchema": {"type": "object"},
        "startStepId": "call",
        "limits": {"timeoutSeconds": 3600, "maxTransitions": 4},
        "steps": {
            "call": {
                "type": "mcp", "toolRef": {"namespace": "device", "name": "demo"},
                "arguments": {}, "saveAs": "demo", "timeoutSeconds": 120, "next": "finish",
            },
            "finish": {"type": "end"},
        },
        "output": {},
    }
    engine = _database()
    with Session(engine) as session:
        user, card = _seed(session, definition)
        run = create_run(session, user_id=user.id, card_id=card.id, device_id="device", input_value={})
        advance_run(session, run.id)
        step = session.exec(select(WorkflowStepRun).where(WorkflowStepRun.run_id == run.id)).one()
        step.deadline_at = 1
        session.add(step)
        confirmation = request_confirmation(
            session,
            run=run,
            step_id="call",
            confirmation_type="forced",
            risk_summary="approve this immutable run",
            timeout_seconds=1800,
            next_step_id=RUN_CONFIRMATION_SCOPE,
        )
        session.commit()

        decide_confirmation(session, run=run, user_id=user.id, approved=True)
        session.refresh(step)
        assert run.status == "waiting_device"
        assert step.status == "dispatch_pending"
        assert step.deadline_at > confirmation.decided_at + 100
        assert confirmation_granted(session, run.id, "call")
        assert confirmation_granted(session, run.id, "a_later_destructive_step")


def test_ai_mediated_dispatch_confirmation_can_resume_the_pending_step():
    definition = {
        "schemaVersion": 1,
        "inputSchema": {"type": "object"},
        "startStepId": "call",
        "limits": {"timeoutSeconds": 3600, "maxTransitions": 4},
        "steps": {
            "call": {
                "type": "mcp", "toolRef": {"namespace": "device", "name": "demo"},
                "arguments": {}, "saveAs": "demo", "timeoutSeconds": 120, "next": "finish",
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
            actor=RunActorContext(actor_type="ai", actor_id="9"),
        )
        advance_run(session, run.id)
        request_confirmation(
            session,
            run=run,
            step_id="call",
            confirmation_type="user_via_ai_dispatch",
            risk_summary="ask the user",
            timeout_seconds=1800,
            next_step_id=RUN_CONFIRMATION_SCOPE,
        )
        session.commit()
        assert run.status == "waiting_confirmation"

        decide_confirmation(session, run=run, user_id=user.id, approved=True)
        assert run.status == "waiting_device"
        assert confirmation_granted(session, run.id, "call")


def test_observed_target_resolver_requires_one_fresh_semantic_match():
    step = {
        "targetResolver": {
            "items": "${steps.observe.result.items}",
            "text": "发布",
            "kind": "interactive",
            "exact": True,
        },
    }
    context = {
        "input": {}, "run": {}, "device": {},
        "steps": {"observe": {"result": {"items": [
            {"id": "e43", "text": "发布说明", "kind": "text"},
            {"id": "e44", "text": "发布", "kind": "interactive", "disabled": False},
        ]}}},
    }
    assert resolve_target_arguments(step, context, {"action": "click", "ref": "stale"}) == {
        "action": "click", "ref": "e44",
    }


def test_end_steps_can_report_published_or_manual_fallback_outcomes():
    definition = {
        "schemaVersion": 1,
        "inputSchema": {"type": "object", "properties": {"verified": {"type": "boolean"}}},
        "startStepId": "check",
        "limits": {"timeoutSeconds": 60, "maxTransitions": 3},
        "steps": {
            "check": {
                "type": "condition", "expression": {
                    "op": "eq", "left": "${input.verified}", "right": True,
                },
                "onTrue": "published", "onFalse": "manual",
            },
            "published": {"type": "end", "output": {"status": "published"}},
            "manual": {"type": "end", "output": {"status": "manual_required"}},
        },
        "output": {"status": "unknown"},
    }
    engine = _database()
    with Session(engine) as session:
        user, card = _seed(session, definition)
        run = create_run(
            session, user_id=user.id, card_id=card.id, device_id="device", input_value={"verified": False},
        )
        advance_run(session, run.id)
        advance_run(session, run.id)
        assert json.loads(run.output_json) == {"status": "manual_required"}


def test_ai_step_callback_parameters_continue_as_a_step_result():
    definition = {
        "schemaVersion": 1,
        "inputSchema": {"type": "object"},
        "startStepId": "review",
        "limits": {"timeoutSeconds": 60, "maxTransitions": 4},
        "steps": {
            "review": {
                "type": "mcp", "toolRef": {"namespace": "device", "name": AI_INTERVENTION_TOOL},
                "arguments": {"prompt": "核对并补充参数"}, "saveAs": "review",
                "timeoutSeconds": 30, "next": "finish", "onError": "fail",
            },
            "finish": {"type": "end"},
        },
        "output": {"value": "${steps.review.result.value}"},
    }
    engine = _database()
    with Session(engine) as session:
        user, card = _seed(session, definition)
        run = create_validated_run(
            session, user_id=user.id, card_id=card.id, device_id="device", input_value={},
            actor=RunActorContext(actor_type="ai", actor_id="9"),
        )
        advance_interactive_run(session, run.id)
        session.refresh(run)
        assert run.status == "waiting_ai"
        respond_ai_interaction(
            session, run=run, user_id=user.id, ai_config_id=9, approved=True,
            parameters={"value": 42}, message="checked",
        )
        advance_run(session, run.id)
        session.refresh(run)
        assert run.status == "succeeded"
        assert json.loads(run.output_json) == {"value": 42}


def test_user_confirmation_timeout_uses_denied_branch():
    definition = {
        "schemaVersion": 1, "inputSchema": {"type": "object"}, "startStepId": "confirm",
        "steps": {
            "confirm": {
                "type": "confirm", "message": "请用户确认", "timeoutSeconds": 1,
                "next": "approved", "onDenied": "denied",
            },
            "approved": {"type": "end"},
            "denied": {"type": "end"},
        },
        "limits": {"timeoutSeconds": 60, "maxTransitions": 4}, "output": {},
    }
    engine = _database()
    with Session(engine) as session:
        user, card = _seed(session, definition)
        run = create_run(session, user_id=user.id, card_id=card.id, device_id="device", input_value={})
        advance_interactive_run(session, run.id)
        confirmation = session.exec(select(WorkflowConfirmation).where(
            WorkflowConfirmation.run_id == run.id,
        )).one()

        assert expire_confirmations(session, now=confirmation.expires_at + 1) == 1
        session.refresh(run)
        assert run.status == "running"
        assert run.current_step_id == "denied"


def test_user_confirmation_notification_snapshot_and_resolution_are_durable():
    definition = {
        "schemaVersion": 1, "inputSchema": {"type": "object"}, "startStepId": "confirm",
        "steps": {
            "confirm": {"type": "confirm", "message": "发布前请核对公开内容", "next": "finish"},
            "finish": {"type": "end"},
        },
        "limits": {"timeoutSeconds": 60, "maxTransitions": 3}, "output": {},
    }
    engine = _database()
    with Session(engine) as session:
        user, card = _seed(session, definition)
        run = create_run(
            session, user_id=user.id, card_id=card.id, device_id="device", input_value={"secret": "hidden"},
            actor=RunActorContext(actor_type="ai", actor_id="9"),
        )
        advance_interactive_run(session, run.id)
        item = session.exec(select(WorkflowConfirmation).where(
            WorkflowConfirmation.run_id == run.id,
        )).one()

        snapshot = pending_notifications(session, user_id=user.id, now=item.created_at)
        assert len(snapshot) == 1
        assert snapshot[0]["actor_name"] == "Running AI"
        assert snapshot[0]["card_name"] == "Test"
        assert "input" not in snapshot[0]
        assert "arguments" not in snapshot[0]
        requested, resolved = notification_events_since(session, since=item.created_at - 1)
        assert [row["confirmation_id"] for row in requested] == [item.id]
        assert resolved == []

        decide_confirmation(session, run=run, user_id=user.id, approved=False)
        requested, resolved = notification_events_since(session, since=item.created_at - 1)
        assert requested == []
        assert resolved[0]["confirmation_id"] == item.id
        assert resolved[0]["status"] == "denied"
        assert pending_notifications(session, user_id=user.id) == []


def test_ai_interaction_enqueues_a_durable_ai_turn(monkeypatch):
    definition = {
        "schemaVersion": 1, "inputSchema": {"type": "object"}, "startStepId": "review",
        "steps": {
            "review": {
                "type": "mcp", "toolRef": {"namespace": "device", "name": AI_INTERVENTION_TOOL},
                "arguments": {"prompt": "核对发布结果"}, "saveAs": "review",
                "timeoutSeconds": 30, "next": "finish", "onError": "fail",
            },
            "finish": {"type": "end"},
        },
        "limits": {"timeoutSeconds": 60, "maxTransitions": 3}, "output": {},
    }
    engine = _database()
    with Session(engine) as session:
        user, card = _seed(session, definition)
        run = create_run(session, user_id=user.id, card_id=card.id, device_id="device", input_value={})
        advance_interactive_run(session, run.id)
        workflow_run_id = run.id
    monkeypatch.setattr("api.services.workflows.ai_interaction_notifier.engine", engine)

    assert process_pending_ai_interactions() == 1
    with Session(engine) as session:
        notice = session.exec(select(ChatMessage).where(ChatMessage.session_id == f"workflow_interaction_{workflow_run_id}")).one()
        queued = session.exec(select(ChatRun).where(ChatRun.session_id == notice.session_id)).one()
        confirmation = session.exec(select(WorkflowConfirmation).where(
            WorkflowConfirmation.run_id == workflow_run_id,
        )).one()
        assert "请核对说明与当前上下文" in notice.content
        assert queued.status == "queued"
        assert confirmation.notified_at is not None
        assert confirmation.notification_run_id == queued.run_id


def test_user_confirmation_never_creates_takeover_conversation(monkeypatch):
    definition = {
        "schemaVersion": 1, "inputSchema": {"type": "object"}, "startStepId": "confirm",
        "steps": {
            "confirm": {"type": "confirm", "message": "请确认继续", "next": "finish"},
            "finish": {"type": "end"},
        },
        "limits": {"timeoutSeconds": 60, "maxTransitions": 3}, "output": {},
    }
    engine = _database()
    with Session(engine) as session:
        user, card = _seed(session, definition)
        run = create_run(
            session,
            user_id=user.id,
            card_id=card.id,
            device_id="device",
            input_value={},
            actor=RunActorContext(actor_type="ai", actor_id="9"),
        )
        advance_interactive_run(session, run.id)
        workflow_run_id = run.id
    monkeypatch.setattr("api.services.workflows.ai_interaction_notifier.engine", engine)

    assert process_pending_ai_interactions() == 0
    with Session(engine) as session:
        assert session.exec(select(ChatSession)).all() == []
        assert session.exec(select(ChatRun)).all() == []
        confirmation = session.exec(select(WorkflowConfirmation).where(
            WorkflowConfirmation.run_id == workflow_run_id,
        )).one()
        assert confirmation.notification_run_id == ""
        assert confirmation.notified_at is None


def test_original_chat_wait_returns_only_after_workflow_terminal(monkeypatch):
    definition = {
        "schemaVersion": 1, "inputSchema": {"type": "object"}, "startStepId": "finish",
        "steps": {"finish": {"type": "end"}},
        "limits": {"timeoutSeconds": 60, "maxTransitions": 2}, "output": {"ok": True},
    }
    engine = _database()
    with Session(engine) as session:
        user, card = _seed(session, definition)
        run = create_run(
            session, user_id=user.id, card_id=card.id, device_id="device", input_value={}
        )
        workflow_run_id = run.id
    monkeypatch.setattr("tools.automation.engine", engine)
    sleeps = []

    def finish_on_first_poll(_seconds):
        sleeps.append(True)
        with Session(engine) as session:
            advance_interactive_run(session, workflow_run_id)

    monkeypatch.setattr("tools.automation.time.sleep", finish_on_first_poll)

    payload = _wait_for_original_chat_run(workflow_run_id, 9, poll_seconds=0.1)

    assert sleeps == [True]
    assert payload["status"] == "succeeded"
    assert payload["resumed_in_original_chat"] is True


def test_stopping_origin_chat_cancels_parked_workflow(monkeypatch):
    definition = {
        "schemaVersion": 1, "inputSchema": {"type": "object"}, "startStepId": "finish",
        "steps": {"finish": {"type": "end"}},
        "limits": {"timeoutSeconds": 60, "maxTransitions": 2}, "output": {},
    }
    engine = _database()
    with Session(engine) as session:
        user, card = _seed(session, definition)
        session.add(ChatRun(
            run_id="chat-run-stopped", user_id=user.id, ai_config_id=9,
            ai_kind="core", session_id="chat-session", status="running",
            stop_requested=True,
        ))
        run = create_run(
            session,
            user_id=user.id,
            card_id=card.id,
            device_id="device",
            input_value={},
            actor=RunActorContext(
                actor_type="ai",
                actor_id="9",
                initial_variables={
                    "_chat_origin": {"run_id": "chat-run-stopped", "session_id": "chat-session"}
                },
            ),
        )
        workflow_run_id = run.id
    monkeypatch.setattr("tools.automation.engine", engine)

    payload = _wait_for_original_chat_run(workflow_run_id, 9, poll_seconds=0.1)

    assert payload["status"] == "cancelled"
    assert payload["resumed_in_original_chat"] is True


def test_run_preflight_rejects_offline_unbound_or_missing_mcp_device():
    definition = {
        "schemaVersion": 1, "inputSchema": {"type": "object"}, "startStepId": "call",
        "steps": {
            "call": {
                "type": "mcp", "toolRef": {"namespace": "device", "name": "demo"},
                "arguments": {}, "saveAs": "demo", "next": "finish",
            },
            "finish": {"type": "end"},
        },
        "limits": {"timeoutSeconds": 60, "maxTransitions": 4}, "output": {},
    }
    engine = _database()
    with Session(engine) as session:
        user, card = _seed(
            session, definition, tool_contracts={"demo": {}}, contract_device_ids=["device"],
        )
        device = session.exec(select(DevicePresence).where(DevicePresence.device_id == "device")).one()
        device.online = False
        session.add(device)
        session.commit()
        with pytest.raises(ValueError, match="DEVICE_OFFLINE"):
            create_validated_run(session, user_id=user.id, card_id=card.id, device_id="device", input_value={})

        device.online = True
        device.tool_defs_json = "{}"
        session.add(device)
        session.commit()
        with pytest.raises(ValueError, match="TOOL_NOT_AVAILABLE"):
            create_validated_run(session, user_id=user.id, card_id=card.id, device_id="device", input_value={})

        session.add(DevicePresence(user_id=user.id, device_id="other", online=True))
        session.commit()
        with pytest.raises(ValueError, match="DEVICE_NOT_BOUND_TO_CARD"):
            create_validated_run(session, user_id=user.id, card_id=card.id, device_id="other", input_value={})


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


@pytest.mark.parametrize(
    "result",
    [
        {"success": False, "errorCode": "ELEMENT_NOT_FOUND", "error": "未找到目标元素"},
        {"result": {"success": False, "code": "WINDOW_CLOSED", "error": "窗口已关闭"}},
    ],
)
def test_tool_level_failure_cannot_be_recorded_as_success(result):
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
                "saveAs": "demo",
                "timeoutSeconds": 10,
                "next": "finish",
            },
            "finish": {"type": "end"},
        },
        "output": {},
    }
    engine = _database()
    with Session(engine) as session:
        user, card = _seed(session, definition, tool_contracts={"demo": {"destructive": False}})
        run = create_run(session, user_id=user.id, card_id=card.id, device_id="device", input_value={})
        advance_run(session, run.id)
        step = session.exec(select(WorkflowStepRun).where(WorkflowStepRun.run_id == run.id)).one()

        assert apply_step_result(
            session,
            dispatch_task_id=step.dispatch_task_id,
            success=True,
            result=result,
        )
        session.refresh(run)
        session.refresh(step)
        assert run.status == "failed"
        assert step.status == "failed"
        assert json.loads(run.error_json)["phase"] == "device_result"


def test_active_card_dispatch_rechecks_scope_schema_arguments_and_confirmation(monkeypatch):
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
        card.status = "active"
        session.add(card)
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


def test_publish_freezes_each_mcp_node_against_its_own_device(monkeypatch):
    definition = {
        "steps": {
            "linux": {
                "type": "mcp",
                "toolRef": {"namespace": "device", "deviceId": "device-a", "name": "linux.info"},
            },
            "desktop": {
                "type": "mcp",
                "toolRef": {"namespace": "device", "deviceId": "device-b", "name": "desktop.capture"},
            },
        },
    }
    engine = _database()
    with Session(engine) as session:
        user, _ = _seed(session, {"steps": {}, "inputSchema": {}, "startStepId": "", "limits": {}})
        session.add(DevicePresence(user_id=user.id, device_id="device-a", device_type="linux", online=True))
        session.add(DevicePresence(user_id=user.id, device_id="device-b", device_type="desktop", online=True))
        session.commit()
        schemas = {
            "device-a": {"linux.info": {"input_schema": {"type": "object", "properties": {"path": {"type": "string"}}}}},
            "device-b": {"desktop.capture": {"input_schema": {"type": "object", "properties": {"screen": {"type": "integer"}}}}},
        }
        monkeypatch.setattr(
            "api.services.workflows.card_service.tool_defs_for_agent",
            lambda _user_id, device_id: schemas[device_id],
        )

        contracts, bound_ids = _snapshot_contracts(
            session, user.id, definition, device_ids=["device-a", "device-b"],
        )

        assert bound_ids == ["device-a", "device-b"]
        assert contracts["linux"]["deviceId"] == "device-a"
        assert contracts["desktop"]["deviceId"] == "device-b"
        assert contracts["linux"]["schemaDigest"] != contracts["desktop"]["schemaDigest"]


def test_run_preflight_and_steps_use_each_nodes_bound_device():
    alpha_schema = {"type": "object", "properties": {"path": {"type": "string"}}}
    beta_schema = {"type": "object", "properties": {"screen": {"type": "integer"}}}
    definition = {
        "schemaVersion": 1,
        "inputSchema": {"type": "object"},
        "startStepId": "alpha",
        "limits": {"timeoutSeconds": 60, "maxTransitions": 5},
        "steps": {
            "alpha": {
                "type": "mcp",
                "toolRef": {
                    "namespace": "device", "deviceId": "device-a", "name": "alpha",
                    "schemaDigest": schema_digest(alpha_schema), "provider": "linux",
                },
                "arguments": {}, "saveAs": "alpha_result", "next": "beta",
            },
            "beta": {
                "type": "mcp",
                "toolRef": {
                    "namespace": "device", "deviceId": "device-b", "name": "beta",
                    "schemaDigest": schema_digest(beta_schema), "provider": "desktop",
                },
                "arguments": {}, "saveAs": "beta_result", "next": "finish",
            },
            "finish": {"type": "end"},
        },
        "output": {},
    }
    contracts = {
        "alpha": {
            "name": "alpha", "deviceId": "device-a", "schemaDigest": schema_digest(alpha_schema),
            "provider": "linux", "providers": ["linux"], "destructive": False,
        },
        "beta": {
            "name": "beta", "deviceId": "device-b", "schemaDigest": schema_digest(beta_schema),
            "provider": "desktop", "providers": ["desktop"], "destructive": False,
        },
    }
    engine = _database()
    with Session(engine) as session:
        user, card = _seed(
            session, definition, tool_contracts=contracts,
            contract_device_ids=["device-a", "device-b"],
        )
        session.add(DevicePresence(
            user_id=user.id, device_id="device-a", device_type="linux", online=True,
            tool_defs_json=json.dumps({"alpha": {"input_schema": alpha_schema}}),
        ))
        session.add(DevicePresence(
            user_id=user.id, device_id="device-b", device_type="desktop", online=True,
            tool_defs_json=json.dumps({"beta": {"input_schema": beta_schema}}),
        ))
        session.commit()

        run = create_validated_run(
            session, user_id=user.id, card_id=card.id, device_id="device-a", input_value={},
        )
        advance_run(session, run.id)
        first = session.exec(select(WorkflowStepRun).where(WorkflowStepRun.run_id == run.id)).one()
        assert step_run_device_id(session, first) == "device-a"

        assert apply_step_result(session, dispatch_task_id=first.dispatch_task_id, success=True, result={})
        advance_run(session, run.id)
        steps = session.exec(
            select(WorkflowStepRun).where(WorkflowStepRun.run_id == run.id).order_by(WorkflowStepRun.attempt, WorkflowStepRun.step_id)
        ).all()
        second = next(item for item in steps if item.step_id == "beta")
        assert step_run_device_id(session, second) == "device-b"
