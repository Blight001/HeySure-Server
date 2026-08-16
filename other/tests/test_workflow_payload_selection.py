import pytest

from api.services.workflows.compiler import WorkflowValidationError
from api.services.workflows.payload_selection import select_card_payload


def _definition():
    return {
        "schemaVersion": 1,
        "name": "Long card",
        "startStepId": "one",
        "steps": {
            "one": {"type": "delay", "delaySeconds": 1, "next": "two"},
            "two": {"type": "delay", "delaySeconds": 1, "next": "three"},
            "three": {"type": "delay", "delaySeconds": 1, "next": "finish"},
            "finish": {"type": "end"},
        },
        "limits": {"timeoutSeconds": 30, "maxTransitions": 5},
        "output": {},
    }


def _payload(*, version=False):
    payload = {
        "id": "wcard_1",
        "name": "Long card",
        "description": "description",
        "definition": _definition(),
        "latest_version_id": "wver_1",
    }
    if version:
        payload["version"] = {
            "id": "wver_1",
            "definition_digest": "sha256:test",
            "definition": _definition(),
        }
    return payload


def test_card_get_without_selectors_preserves_legacy_payload():
    payload = _payload(version=True)

    selected = select_card_payload(payload, {"action": "get", "card_id": "wcard_1"})

    assert selected is payload
    assert "selection" not in selected
    assert "pagination" not in selected


def test_card_get_can_filter_fields_and_page_definition_steps():
    selected = select_card_payload(_payload(version=True), {
        "fields": {
            "card": ["id", "name", "definition", "version"],
            "definition": ["schemaVersion", "startStepId", "steps"],
            "version": ["id", "definition"],
        },
        "step_offset": 1,
        "step_limit": 2,
    })

    assert list(selected) == ["id", "name", "definition", "version", "selection", "pagination"]
    assert list(selected["definition"]["steps"]) == ["two", "three"]
    assert list(selected["version"]["definition"]["steps"]) == ["two", "three"]
    assert selected["pagination"]["definition"] == {
        "mode": "page",
        "total_steps": 4,
        "returned_steps": 2,
        "returned_step_ids": ["two", "three"],
        "has_more": True,
        "next_offset": 3,
        "offset": 1,
        "limit": 2,
    }
    assert selected["selection"]["card_fields"] == ["id", "name", "definition", "version"]
    assert selected["selection"]["definition_fields"] == ["schemaVersion", "startStepId", "steps"]
    assert selected["selection"]["version_fields"] == ["id", "definition"]


def test_card_get_step_ids_preserve_requested_order_and_report_missing_ids():
    selected = select_card_payload(_payload(), {
        "step_ids": ["three", "missing", "one"],
    })

    assert list(selected["definition"]["steps"]) == ["three", "one"]
    metadata = selected["pagination"]["definition"]
    assert metadata["mode"] == "ids"
    assert metadata["returned_step_ids"] == ["three", "one"]
    assert metadata["missing_step_ids"] == ["missing"]


def test_card_get_tail_reports_that_earlier_steps_were_omitted():
    selected = select_card_payload(_payload(), {"tail": 2})

    assert list(selected["definition"]["steps"]) == ["three", "finish"]
    assert selected["pagination"]["definition"]["has_more"] is True
    assert selected["pagination"]["definition"]["offset"] == 2


@pytest.mark.parametrize("args", [
    {"step_ids": ["one"], "tail": 1},
    {"fields": {"card": ["id"]}, "step_limit": 1},
    {"fields": {"card": ["id", "definition"], "definition": ["schemaVersion"]}, "tail": 1},
    {"fields": {"card": ["version"], "version": ["id"]}, "tail": 1},
    {"fields": {"unknown": ["id"]}},
])
def test_card_get_rejects_ambiguous_or_contradictory_selection(args):
    with pytest.raises(WorkflowValidationError):
        select_card_payload(_payload(version=True), args)


def test_automation_schema_exposes_bounded_card_get_selectors():
    from tools.automation import AUTOMATION_MANAGE_SCHEMA

    properties = AUTOMATION_MANAGE_SCHEMA["properties"]
    assert {"fields", "step_ids", "step_offset", "step_limit", "tail"} <= set(properties)
    assert properties["step_limit"]["maximum"] == 100
    assert "省略时保持原完整返回" in properties["fields"]["description"]


def test_automation_card_get_applies_selection_without_changing_legacy_card_payload(monkeypatch):
    from tools import automation

    class FakeSession:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return None

    source = _payload()
    monkeypatch.setattr(automation, "Session", lambda _engine: FakeSession())
    monkeypatch.setattr(automation, "_accessible_card", lambda *_args, **_kwargs: object())
    monkeypatch.setattr(automation, "card_payload", lambda _card: source)

    selected = automation._manage_card(1, {
        "action": "get",
        "card_id": "wcard_1",
        "step_limit": 1,
    }, None)

    assert list(selected["definition"]["steps"]) == ["one"]
    assert selected["pagination"]["definition"]["next_offset"] == 1
    assert len(source["definition"]["steps"]) == 4


def test_card_get_accepts_friendly_id_aliases():
    selected = select_card_payload(_payload(), {"fields": {"card": ["card_id", "version_id"]}})
    assert selected["card_id"] == "wcard_1"
    assert selected["version_id"] == "wver_1"


def test_card_get_unknown_field_lists_valid_fields():
    with pytest.raises(WorkflowValidationError) as raised:
        select_card_payload(_payload(), {"fields": {"card": ["missing"]}})
    error = raised.value.errors[0]
    assert "valid fields:" in error
    assert "card_id" in error and "latest_version_id" in error
