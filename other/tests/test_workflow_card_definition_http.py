"""HTTP-layer contracts for safe workflow definition reads and changes."""

import json

import pytest
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient
from sqlmodel import Session, SQLModel, create_engine, select

from api.database import get_session
from api.models import AssistantAIConfig, User, WorkflowCard, WorkflowCardVersion
from api.services.workflows.card_service import create_card
from api.services.workflows.schemas import (
    CardCreate,
    DefinitionPatchRequest,
    DefinitionReplaceRequest,
)
from gateway.routers import workflow_cards


def _database():
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False})
    SQLModel.metadata.create_all(engine, tables=[
        User.__table__, AssistantAIConfig.__table__, WorkflowCard.__table__,
        WorkflowCardVersion.__table__,
    ])
    return engine


def _user(session: Session) -> User:
    row = User(name="HTTP", account="workflow-http", hashed_password="x")
    session.add(row)
    session.commit()
    session.refresh(row)
    return row


def _definition(value: int = 1):
    return {
        "schemaVersion": 1,
        "inputSchema": {"type": "object"},
        "startStepId": "finish",
        "steps": {"finish": {"type": "end", "output": {"value": value}}},
        "limits": {"timeoutSeconds": 30, "maxTransitions": 3},
        "output": {},
    }


def _versions(session: Session, card_id: str):
    return session.exec(
        select(WorkflowCardVersion)
        .where(WorkflowCardVersion.card_id == card_id)
        .order_by(WorkflowCardVersion.version_number)
    ).all()


def test_patch_http_dry_run_token_commits_the_validated_payload(monkeypatch):
    with Session(_database()) as session:
        user = _user(session)
        monkeypatch.setattr(workflow_cards, "get_current_user", lambda *_: user)
        card = create_card(session, user.id, CardCreate(name="Patch HTTP", definition=_definition()))
        base_id = card.latest_version_id
        operation = {"op": "replace", "path": "/steps/finish/output/value", "value": 7}

        preview = workflow_cards.patch_definition(
            card.id,
            DefinitionPatchRequest(
                base_version_id=base_id, operations=[operation], dry_run=True,
            ),
            session,
            None,
        )

        assert preview["committed"] is False
        assert preview["version_created"] is False
        assert preview["preview_token"]
        assert len(_versions(session, card.id)) == 1

        committed = workflow_cards.patch_definition(
            card.id,
            DefinitionPatchRequest(
                base_version_id=base_id, preview_token=preview["preview_token"],
            ),
            session,
            None,
        )

        versions = _versions(session, card.id)
        assert committed["committed"] is True
        assert committed["version"]["definition"]["steps"]["finish"]["output"]["value"] == 7
        assert len(versions) == 2
        assert json.loads(versions[0].definition_json)["steps"]["finish"]["output"]["value"] == 1


def test_replace_http_dry_run_token_preserves_optimistic_lock(monkeypatch):
    with Session(_database()) as session:
        user = _user(session)
        monkeypatch.setattr(workflow_cards, "get_current_user", lambda *_: user)
        card = create_card(session, user.id, CardCreate(name="Replace HTTP", definition=_definition()))
        base_id = card.latest_version_id

        preview = workflow_cards.replace_definition(
            card.id,
            DefinitionReplaceRequest(
                base_version_id=base_id, definition=_definition(9), dry_run=True,
            ),
            session,
            None,
        )
        committed = workflow_cards.replace_definition(
            card.id,
            DefinitionReplaceRequest(
                base_version_id=base_id, preview_token=preview["preview_token"],
            ),
            session,
            None,
        )

        assert committed["version"]["version_number"] == 2
        with pytest.raises(HTTPException) as raised:
            workflow_cards.replace_definition(
                card.id,
                DefinitionReplaceRequest(
                    base_version_id=base_id, preview_token=preview["preview_token"],
                ),
                session,
                None,
            )
        assert raised.value.status_code == 422
        assert "card changed" in " ".join(raised.value.detail["errors"])
        assert len(_versions(session, card.id)) == 2


def _large_definition(step_count: int = 200):
    steps = {}
    for index in range(step_count - 1):
        steps[f"step_{index:03d}"] = {
            "type": "delay",
            "delaySeconds": 0,
            "next": f"step_{index + 1:03d}" if index + 1 < step_count - 1 else "finish",
        }
    steps["finish"] = {"type": "end"}
    return {
        "schemaVersion": 1,
        "inputSchema": {"type": "object"},
        "startStepId": "step_000",
        "steps": steps,
        "limits": {"timeoutSeconds": 300, "maxTransitions": step_count},
        "output": {},
    }


def test_definition_http_returns_all_steps_or_a_deterministic_page(monkeypatch):
    with Session(_database()) as session:
        user = _user(session)
        monkeypatch.setattr(workflow_cards, "get_current_user", lambda *_: user)
        card = create_card(session, user.id, CardCreate(
            name="Large definition", definition=_large_definition(),
        ))

        app = FastAPI()
        app.include_router(workflow_cards.router, prefix=workflow_cards.PREFIX)
        app.dependency_overrides[get_session] = lambda: session
        client = TestClient(app)
        complete_response = client.get(f"{workflow_cards.PREFIX}/{card.id}/definition")
        page_response = client.get(
            f"{workflow_cards.PREFIX}/{card.id}/definition",
            params={"version_id": card.latest_version_id, "step_offset": 100, "step_limit": 75},
        )
        assert complete_response.status_code == 200
        assert page_response.status_code == 200
        complete = complete_response.json()
        page = page_response.json()

        assert len(complete["definition"]["steps"]) == 200
        assert complete["step_page"] == {
            "offset": 0, "limit": None, "returned": 200, "total": 200,
            "has_more": False, "next_offset": None, "definition_complete": True,
        }
        assert list(page["definition"]["steps"])[0] == "step_100"
        assert len(page["definition"]["steps"]) == 75
        assert page["step_page"]["next_offset"] == 175
        assert page["step_page"]["definition_complete"] is False
        assert page["id"] == card.latest_version_id
