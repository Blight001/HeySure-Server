import json

import pytest
from sqlmodel import Session, SQLModel, create_engine

from api.models import User, WorkflowCard, WorkflowCardVersion
from api.services.workflows.card_references import resolve_card_references
from api.services.workflows.compiler import WorkflowValidationError, compile_definition


def _child_definition():
    return {
        "schemaVersion": 1,
        "inputSchema": {
            "type": "object",
            "properties": {"value": {"type": "string"}},
            "required": ["value"],
        },
        "startStepId": "wait",
        "steps": {
            "wait": {"type": "delay", "delaySeconds": 0, "next": "finish"},
            "finish": {"type": "end", "output": {"echo": "${input.value}"}},
        },
        "limits": {"timeoutSeconds": 30, "maxTransitions": 10},
        "output": {},
    }


def test_compile_expands_referenced_card_and_rewrites_input_output_templates():
    parent = {
        "schemaVersion": 1,
        "inputSchema": {
            "type": "object",
            "properties": {"name": {"type": "string"}},
            "required": ["name"],
        },
        "startStepId": "child",
        "steps": {
            "child": {
                "type": "card",
                "title": "子流程",
                "cardRef": {"id": "wcard_child", "versionId": "wver_child", "name": "子卡片"},
                "_definition": _child_definition(),
                "input": {"value": "${input.name}"},
                "saveAs": "child_result",
                "next": "finish",
                "onError": "failed",
            },
            "failed": {"type": "end", "output": {"status": "failed"}},
            "finish": {"type": "end", "output": {"echo": "${steps.child_result.result.echo}"}},
        },
        "limits": {"timeoutSeconds": 60, "maxTransitions": 20},
        "output": {},
    }

    compiled = compile_definition(parent)["definition"]

    assert compiled["steps"]["child"]["type"] == "_card_enter"
    assert compiled["steps"]["child"]["_nestedLimits"] == {
        "timeoutSeconds": 30, "maxTransitions": 10,
    }
    nested_return = next(step for step in compiled["steps"].values() if step.get("type") == "_card_return")
    assert nested_return["output"] == {
        "echo": "${steps.child_result.result.input.value}",
    }
    assert nested_return["next"] == "finish"
    assert compiled["steps"]["finish"]["output"]["echo"] == "${steps.child_result.result.echo}"


def test_resolve_pins_latest_version_and_rejects_indirect_card_cycle():
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False})
    SQLModel.metadata.create_all(engine, tables=[
        User.__table__, WorkflowCard.__table__, WorkflowCardVersion.__table__,
    ])
    with Session(engine) as session:
        user = User(name="Nested", account="nested", hashed_password="x")
        session.add(user)
        session.commit()
        session.refresh(user)
        parent = WorkflowCard(id="parent", user_id=user.id, created_by=user.id, name="Parent")
        child = WorkflowCard(id="child", user_id=user.id, created_by=user.id, name="Child")
        version = WorkflowCardVersion(
            id="child-v1", card_id=child.id, version_number=1,
            definition_json=json.dumps({
                "schemaVersion": 1, "startStepId": "done",
                "steps": {"done": {"type": "end"}},
            }),
            definition_digest="digest", published_by=user.id,
        )
        session.add_all([parent, child, version])
        session.flush()
        child.latest_version_id = version.id
        session.add(child)
        session.commit()
        source = {
            "schemaVersion": 1, "startStepId": "call",
            "steps": {"call": {"type": "card", "cardRef": {"id": "child"}}},
        }
        hydrated, pinned = resolve_card_references(
            session, user_id=user.id, parent_card_id=parent.id, definition=source,
        )
        assert pinned["steps"]["call"]["cardRef"]["versionId"] == version.id
        assert hydrated["steps"]["call"]["_definition"]["startStepId"] == "done"

        version.definition_json = json.dumps({
            "schemaVersion": 1, "startStepId": "nested",
            "steps": {"nested": {"type": "_card_enter", "_nestedCardId": parent.id}},
        })
        session.add(version)
        session.commit()
        with pytest.raises(WorkflowValidationError, match="indirect card cycle"):
            resolve_card_references(
                session, user_id=user.id, parent_card_id=parent.id, definition=source,
            )
