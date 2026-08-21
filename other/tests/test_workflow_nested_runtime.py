import json

import pytest
from sqlmodel import Session, SQLModel, create_engine

from api.models import (
    AssistantAIConfig, User, WorkflowAuditEvent, WorkflowCard,
    WorkflowCardVersion, WorkflowRun, WorkflowStepRun, WorkflowConfirmation,
)
from api.services.workflows.compiler import compile_definition, definition_digest
from api.services.workflows.run_service import advance_run, cancel_run, create_run, RunActorContext


def _database():
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False})
    SQLModel.metadata.create_all(engine, tables=[
        User.__table__, AssistantAIConfig.__table__, WorkflowCard.__table__,
        WorkflowCardVersion.__table__, WorkflowRun.__table__, WorkflowStepRun.__table__,
        WorkflowConfirmation.__table__, WorkflowAuditEvent.__table__,
    ])
    return engine


def _seed(session: Session, definition: dict):
    user = User(name="Nested", account="nested-runtime", hashed_password="x")
    session.add(user)
    session.commit()
    session.refresh(user)
    session.add_all([
        AssistantAIConfig(id=7, user_id=user.id, name="Allowed"),
        AssistantAIConfig(id=9, user_id=user.id, name="Denied"),
    ])
    card = WorkflowCard(
        id="parent", user_id=user.id, created_by=user.id, name="Parent", status="published",
    )
    version = WorkflowCardVersion(
        id="parent-v1", card_id=card.id, version_number=1,
        definition_json=json.dumps(definition), definition_digest=definition_digest(definition),
        published_by=user.id,
    )
    session.add_all([card, version])
    session.flush()
    card.latest_version_id = version.id
    session.add(card)
    session.commit()
    return user, card


def test_nested_card_enforces_transition_limit_and_propagates_to_on_error():
    child = {
        "schemaVersion": 1, "inputSchema": {"type": "object"}, "startStepId": "wait",
        "steps": {
            "wait": {"type": "delay", "delaySeconds": 0, "next": "finish"},
            "finish": {"type": "end", "output": {"ok": True}},
        },
        "limits": {"timeoutSeconds": 30, "maxTransitions": 1}, "output": {},
    }
    parent = {
        "schemaVersion": 1, "inputSchema": {"type": "object"}, "startStepId": "child",
        "steps": {
            "child": {
                "type": "card", "cardRef": {"id": "child", "versionId": "child-v1"},
                "_definition": child, "input": {}, "saveAs": "child_result",
                "next": "finish", "onError": "recover",
            },
            "recover": {"type": "end", "output": {"code": "${steps.child_result.error.code}"}},
            "finish": {"type": "end", "output": {"ok": True}},
        },
        "limits": {"timeoutSeconds": 60, "maxTransitions": 10}, "output": {},
    }
    engine = _database()
    with Session(engine) as session:
        user, card = _seed(session, compile_definition(parent)["definition"])
        run = create_run(session, user_id=user.id, card_id=card.id, device_id="", input_value={})
        for _ in range(4):
            advance_run(session, run.id)
        session.refresh(run)
        assert run.status == "succeeded"
        assert json.loads(run.output_json) == {"code": "NESTED_CARD_MAX_TRANSITIONS_EXCEEDED"}


def test_nested_card_timeout_uses_parent_on_error_branch():
    child = {
        "schemaVersion": 1, "inputSchema": {"type": "object"}, "startStepId": "wait",
        "steps": {
            "wait": {"type": "delay", "delaySeconds": 0, "next": "finish"},
            "finish": {"type": "end"},
        },
        "limits": {"timeoutSeconds": 30, "maxTransitions": 5}, "output": {},
    }
    parent = {
        "schemaVersion": 1, "inputSchema": {"type": "object"}, "startStepId": "child",
        "steps": {
            "child": {
                "type": "card", "cardRef": {"id": "child"}, "_definition": child,
                "input": {}, "saveAs": "child_result", "next": "finish", "onError": "recover",
            },
            "recover": {"type": "end", "output": {"code": "${steps.child_result.error.code}"}},
            "finish": {"type": "end"},
        },
        "limits": {"timeoutSeconds": 60, "maxTransitions": 10}, "output": {},
    }
    with Session(_database()) as session:
        user, card = _seed(session, compile_definition(parent)["definition"])
        run = create_run(session, user_id=user.id, card_id=card.id, device_id="", input_value={})
        advance_run(session, run.id)
        session.refresh(run)
        variables = json.loads(run.variables_json)
        variables["_nested_cards"][-1]["deadlineAt"] = 1
        run.variables_json = json.dumps(variables)
        session.add(run)
        session.commit()
        advance_run(session, run.id)
        advance_run(session, run.id)
        session.refresh(run)
        assert run.status == "succeeded"
        assert json.loads(run.output_json) == {"code": "NESTED_CARD_TIMEOUT"}


def test_cancelling_parent_run_cancels_active_nested_card():
    child = {
        "schemaVersion": 1, "inputSchema": {"type": "object"}, "startStepId": "wait",
        "steps": {
            "wait": {"type": "delay", "delaySeconds": 30, "next": "finish"},
            "finish": {"type": "end"},
        },
        "limits": {"timeoutSeconds": 60, "maxTransitions": 5}, "output": {},
    }
    parent = {
        "schemaVersion": 1, "inputSchema": {"type": "object"}, "startStepId": "child",
        "steps": {
            "child": {
                "type": "card", "cardRef": {"id": "child"}, "_definition": child,
                "input": {}, "saveAs": "child_result", "next": "finish", "onError": "fail",
            },
            "finish": {"type": "end"},
        },
        "limits": {"timeoutSeconds": 120, "maxTransitions": 10}, "output": {},
    }
    with Session(_database()) as session:
        user, card = _seed(session, compile_definition(parent)["definition"])
        run = create_run(session, user_id=user.id, card_id=card.id, device_id="", input_value={})
        advance_run(session, run.id)
        session.refresh(run)
        assert json.loads(run.variables_json)["_nested_cards"]
        cancel_run(session, run, "user requested cancellation")
        session.refresh(run)
        assert run.status == "cancelled"
        assert json.loads(run.error_json)["code"] == "RUN_CANCELLED"


def test_ai_actor_must_have_access_to_every_referenced_card():
    definition = {
        "schemaVersion": 1, "inputSchema": {"type": "object"}, "startStepId": "child",
        "steps": {
            "child": {
                "type": "_card_enter", "cardRef": {"id": "private-child"},
                "input": {}, "inputSchema": {"type": "object"}, "saveAs": "child",
                "next": "return", "onError": "fail", "_nestedLimits": {},
            },
            "return": {"type": "_card_return", "output": {}, "saveAs": "child", "next": "finish"},
            "finish": {"type": "end"},
        },
        "limits": {"timeoutSeconds": 60, "maxTransitions": 10}, "output": {},
    }
    with Session(_database()) as session:
        user, card = _seed(session, definition)
        session.add(WorkflowCard(
            id="private-child", user_id=user.id, created_by=user.id, name="Private child",
            access_scope="selected", allowed_ai_config_ids_json="[7]", status="active",
        ))
        session.commit()
        with pytest.raises(ValueError, match="NESTED_CARD_ACCESS_DENIED"):
            create_run(
                session, user_id=user.id, card_id=card.id, device_id="", input_value={},
                actor=RunActorContext(actor_type="ai", actor_id="9"),
            )
        allowed = create_run(
            session, user_id=user.id, card_id=card.id, device_id="", input_value={},
            actor=RunActorContext(actor_type="ai", actor_id="7"),
        )
        assert allowed.status == "pending"
