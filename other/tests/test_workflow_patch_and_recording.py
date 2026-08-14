import json

import pytest
from sqlmodel import Session, SQLModel, create_engine, select

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
from api.services.workflows.definition_replace_service import replace_card_definition
from api.services.workflows.recording_service import (
    RecordedToolCall,
    classify_recorded_tool_call,
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


def test_replace_definition_dry_run_validates_and_does_not_create_version():
    with Session(_database()) as session:
        user = _user(session)
        card = create_card(session, user.id, CardCreate(
            name="Replace", description="metadata", tags=["keep"],
            risk_level="normal", definition=_definition(),
        ))
        base = card.latest_version_id
        replacement = _definition()
        replacement["steps"]["finish"]["output"]["value"] = 2

        result = replace_card_definition(
            session, card=card, user_id=user.id, base_version_id=base,
            definition=replacement, dry_run=True,
        )

        session.refresh(card)
        versions = session.exec(
            select(WorkflowCardVersion).where(WorkflowCardVersion.card_id == card.id)
        ).all()
        assert result["validation"]["valid"] is True
        assert result["diff"]["changed_paths"] == ["/steps/finish/output/value"]
        assert result["diff"]["before_digest"] != result["diff"]["after_digest"]
        assert result["version"] is None
        assert card.latest_version_id == base
        assert len(versions) == 1


def test_replace_definition_creates_immutable_version_and_rejects_stale_base():
    with Session(_database()) as session:
        user = _user(session)
        card = create_card(session, user.id, CardCreate(
            name="Replace", description="metadata", tags=["keep"],
            risk_level="normal", definition=_definition(),
        ))
        base = card.latest_version_id
        replacement = _definition()
        replacement["steps"]["finish"]["output"]["value"] = 3

        result = replace_card_definition(
            session, card=card, user_id=user.id, base_version_id=base,
            definition=replacement,
        )

        assert result["version"]["version_number"] == 2
        assert result["version"]["definition"]["steps"]["finish"]["output"]["value"] == 3
        assert session.get(WorkflowCardVersion, base).version_number == 1
        assert (card.name, card.description, card.risk_level) == ("Replace", "metadata", "normal")
        assert json.loads(card.tags_json) == ["keep"]
        with pytest.raises(WorkflowValidationError, match="reload"):
            replace_card_definition(
                session, card=card, user_id=user.id, base_version_id=base,
                definition=replacement,
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


@pytest.mark.parametrize(
    "result, expected_code",
    [
        (
            {
                "success": False,
                "errorCode": "BROWSER_TAKEOVER_REQUIRED",
                "error": "acquire browser control first",
            },
            "BROWSER_TAKEOVER_REQUIRED",
        ),
        (
            {"result": {"ok": False, "code": "CLICK_REJECTED", "message": "not clickable"}},
            "CLICK_REJECTED",
        ),
        (
            {"code": "BROWSER_TAKEOVER_REQUIRED", "detail": "acquire browser control first"},
            "BROWSER_TAKEOVER_REQUIRED",
        ),
        ({"status": "timeout", "detail": "operation timed out"}, "TOOL_REPORTED_FAILURE"),
    ],
)
def test_recording_keeps_business_failures_for_diagnostics_but_marks_them_unrecordable(
    result, expected_code,
):
    with Session(_database()) as session:
        user = _user(session)
        row = start_recording(
            session, user_id=user.id, ai_config_id=None, name="Practice",
            description="", default_device_id="device-a", device_ids=["device-a"],
        )
        call = RecordedToolCall(
            user_id=user.id, ai_config_id=None, tool="browser.click",
            arguments={"ref": "e15"}, result=result,
            success=True, error="", device_id="device-a",
        )
        outcome = classify_recorded_tool_call(call)
        record_completed_tool_call(session, call)
        payload = recording_payload(session, row, include_events=True)

        assert outcome.transport_success is True
        assert outcome.business_success is False
        assert outcome.recordable is False
        assert len(payload["calls"]) == 1
        assert payload["calls"][0]["success"] is False
        assert expected_code in payload["calls"][0]["error"]
        assert payload["calls"][0]["result"] == result


@pytest.mark.parametrize(
    "result",
    [
        {"success": True, "result": {"items": []}},
        {"ok": True, "message": "completed"},
        {"code": "PAGE_STATE", "message": "diagnostic data"},
        {"result": {"status": "ready", "error": "historical field value"}},
    ],
)
def test_recording_does_not_reject_results_without_explicit_failure(result):
    outcome = classify_recorded_tool_call(RecordedToolCall(
        user_id=1, ai_config_id=None, tool="browser.observe", arguments={},
        result=result, success=True, error="", device_id="device-a",
    ))

    assert outcome.transport_success is True
    assert outcome.business_success is True
    assert outcome.recordable is True
