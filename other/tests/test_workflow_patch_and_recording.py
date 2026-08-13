import json

import pytest
from sqlmodel import Session, SQLModel, create_engine

from api.models import (
    AssistantAIConfig,
    User,
    WorkflowCard,
    WorkflowCardVersion,
    WorkflowRecording,
    WorkflowRecordingEvent,
)
from api.services.workflows.card_service import create_card
from api.services.workflows.compiler import WorkflowValidationError
from api.services.workflows.patch_service import patch_card_definition
from api.services.workflows.recording_service import (
    RecordedToolCall,
    record_completed_tool_call,
    recording_payload,
    start_recording,
    stop_recording,
)
from api.services.workflows.schemas import CardCreate


def _database():
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False})
    SQLModel.metadata.create_all(engine, tables=[
        User.__table__, AssistantAIConfig.__table__, WorkflowCard.__table__,
        WorkflowCardVersion.__table__, WorkflowRecording.__table__, WorkflowRecordingEvent.__table__,
    ])
    return engine


def _user(session):
    row = User(name="Patch", account="patch-recording", hashed_password="x")
    session.add(row)
    session.commit()
    session.refresh(row)
    return row


def _definition():
    return {
        "schemaVersion": 1,
        "inputSchema": {"type": "object"},
        "startStepId": "finish",
        "steps": {"finish": {"type": "end", "output": {"value": 1}}},
        "limits": {"timeoutSeconds": 30, "maxTransitions": 3},
        "output": {},
    }


def test_partial_patch_creates_new_version_without_replacing_other_fields():
    with Session(_database()) as session:
        user = _user(session)
        card = create_card(session, user.id, CardCreate(name="Patch", definition=_definition()))
        base = card.latest_version_id

        result = patch_card_definition(
            session,
            card=card,
            user_id=user.id,
            base_version_id=base,
            operations=[{"op": "replace", "path": "/steps/finish/output/value", "value": 2}],
        )

        assert result["changed_paths"] == ["/steps/finish/output/value"]
        assert result["version"]["version_number"] == 2
        assert result["version"]["definition"]["inputSchema"] == {"type": "object"}
        assert result["version"]["definition"]["steps"]["finish"]["output"]["value"] == 2
        with pytest.raises(WorkflowValidationError, match="reload"):
            patch_card_definition(
                session, card=card, user_id=user.id, base_version_id=base,
                operations=[{"op": "replace", "path": "/steps/finish/output/value", "value": 3}],
            )


def test_recording_captures_redacted_calls_and_device_numbers_until_stop():
    with Session(_database()) as session:
        user = _user(session)
        row = start_recording(
            session, user_id=user.id, ai_config_id=None, name="Practice",
            description="", default_device_id="device-a", device_ids=["device-a"],
        )
        record_completed_tool_call(session, RecordedToolCall(
            user_id=user.id, ai_config_id=None, tool="browser.click",
            arguments={"selector": "#publish", "token": "private"}, result={"ok": True},
            success=True, error="", device_id="device-a",
        ))
        stopped = stop_recording(session, user.id, None)
        payload = recording_payload(session, stopped, include_events=True)

        assert row.id == stopped.id
        assert payload["status"] == "stopped"
        assert payload["device_ids"] == ["device-a"]
        assert payload["calls"][0]["tool"] == "browser.click"
        assert payload["calls"][0]["arguments"]["token"] == "[REDACTED]"
