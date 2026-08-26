import math

import pytest
from pydantic import ValidationError

from api.services.remote_control import controller_schema
from api.services.remote_control.controller_schema import (
    ControllerActionMessage,
    TemplateCreate,
    TemplateDocument,
)
from api.services.remote_control.controller_templates import BUILTIN_TEMPLATES, _builtin_document


def _valid_template(**overrides):
    payload = {
        "schema": "remote_controller_template.v1",
        "id": "custom-pad",
        "name": "Custom Pad",
        "deviceTypes": ["desktop"],
        "requiredCapabilities": ["remote_control"],
        "layout": {"columns": 2, "gap": "sm"},
        "controls": [
            {
                "id": "confirm",
                "kind": "button",
                "label": "Confirm",
                "tone": "primary",
                "action": {"type": "key", "key": "Enter"},
            }
        ],
    }
    payload.update(overrides)
    return payload


def test_builtin_templates_are_strict_documents_and_stable():
    assert set(BUILTIN_TEMPLATES) == {"direction", "media", "presentation", "browser", "jibotarm"}
    for template_id in BUILTIN_TEMPLATES:
        document = _builtin_document(template_id)
        assert isinstance(document, TemplateDocument)
        assert document.id == template_id
        assert document.revision == 1
        assert document.builtin is True

    arm = _builtin_document("jibotarm")
    assert arm.device_types == ["custom"]
    assert [control.action.event for control in arm.controls] == [
        f"jibotarm.joint{joint}.position_p" for joint in range(1, 7)
    ]
    assert all(
        control.minimum == 500 and control.maximum == 2500 and control.step == 1
        for control in arm.controls
    )


@pytest.mark.parametrize(
    "mutation",
    [
        {"html": "<script>alert(1)</script>"},
        {"controls": [{
            "id": "bad", "kind": "button", "label": "Bad",
            "action": {"type": "key", "key": "Enter", "shell": "calc.exe"},
        }]},
        {"controls": [{
            "id": "bad", "kind": "button", "label": "Bad",
            "action": {"type": "browser", "action": "navigate", "url": "https://evil.invalid"},
        }]},
        {"controls": [{
            "id": "bad", "kind": "button", "label": "Bad",
            "action": {"type": "key", "key": "UnknownSystemKey"},
        }]},
        {"controls": [{
            "id": "bad", "kind": "button", "label": "Bad",
            "action": {"type": "emit", "event": "rc.start"},
        }]},
    ],
)
def test_template_rejects_unknown_or_executable_payloads(mutation):
    with pytest.raises(ValidationError):
        TemplateCreate.model_validate(_valid_template(**mutation))


def test_template_rejects_duplicate_controls_and_excess_count():
    control = _valid_template()["controls"][0]
    with pytest.raises(ValidationError, match="control ids must be unique"):
        TemplateCreate.model_validate(_valid_template(controls=[control, control]))
    controls = [
        {
            "id": f"button-{index}", "kind": "button", "label": str(index),
            "action": {"type": "key", "key": "Enter"},
        }
        for index in range(controller_schema.MAX_CONTROLS + 1)
    ]
    with pytest.raises(ValidationError):
        TemplateCreate.model_validate(_valid_template(controls=controls))


def test_template_rejects_invalid_capability_and_duplicate_device_type():
    with pytest.raises(ValidationError):
        TemplateCreate.model_validate(_valid_template(requiredCapabilities=["shell.run"]))
    with pytest.raises(ValidationError, match="values must be unique"):
        TemplateCreate.model_validate(_valid_template(deviceTypes=["desktop", "desktop"]))


def test_control_kind_specific_fields_are_strict():
    joystick = {
        "id": "stick", "kind": "joystick", "label": "Stick", "deadZone": 0.1,
        "action": {"type": "emit", "event": "game.axis"},
    }
    native_caps = ["remote_control", "remote_controller_templates"]
    assert TemplateCreate.model_validate(
        _valid_template(controls=[joystick], requiredCapabilities=native_caps)
    ).controls[0].dead_zone == 0.1
    with pytest.raises(ValidationError, match="emit actions require"):
        TemplateCreate.model_validate(_valid_template(controls=[joystick]))
    with pytest.raises(ValidationError, match="non-button controls require an emit action"):
        TemplateCreate.model_validate(_valid_template(
            controls=[{**joystick, "action": {"type": "key", "key": "Enter"}}],
            requiredCapabilities=native_caps,
        ))
    with pytest.raises(ValidationError, match="slider requires"):
        TemplateCreate.model_validate(_valid_template(
            controls=[{
                "id": "speed", "kind": "slider", "label": "Speed",
                "action": {"type": "emit", "event": "game.speed"},
            }],
            requiredCapabilities=native_caps,
        ))
    for invalid_bound in (-1_000_001, 1_000_001):
        with pytest.raises(ValidationError):
            TemplateCreate.model_validate(_valid_template(
                controls=[{
                    "id": "speed", "kind": "slider", "label": "Speed",
                    "min": invalid_bound, "max": 10, "step": 1,
                    "action": {"type": "emit", "event": "game.speed"},
                }],
                requiredCapabilities=native_caps,
            ))


def test_template_enforces_serialized_byte_limit(monkeypatch):
    monkeypatch.setattr(controller_schema, "MAX_TEMPLATE_BYTES", 100)
    with pytest.raises(ValidationError, match="template exceeds"):
        TemplateCreate.model_validate(_valid_template())


def test_controller_action_contract_rejects_non_finite_bounds_and_extra_fields():
    valid = {
        "kind": "controller-action", "v": 1,
        "templateId": "gamepad", "controlId": "left-stick",
        "seq": 4, "phase": "update", "event": "game.axis",
        "value": {"x": -0.5, "y": 1.0}, "ts": 123,
    }
    assert ControllerActionMessage.model_validate(valid).seq == 4
    for payload in (
        {**valid, "value": {"x": 2, "y": 0}},
        {**valid, "value": math.inf},
        {**valid, "value": 1_000_001},
        {**valid, "value": -1_000_001},
        {**valid, "phase": "press"},
        {**valid, "event": "rt.input"},
        {key: value for key, value in valid.items() if key != "event"},
        {**valid, "value": True},
        {**valid, "value": {"text": "undocumented-wrapper"}},
        {**valid, "seq": "4"},
        {**valid, "ts": "123"},
        {**valid, "token": "must-not-travel"},
    ):
        with pytest.raises(ValidationError):
            ControllerActionMessage.model_validate(payload)


def test_template_contract_rejects_coercive_scalar_types():
    for payload in (
        _valid_template(layout={"columns": "2", "gap": "sm"}),
        _valid_template(layout={"columns": True, "gap": "sm"}),
    ):
        with pytest.raises(ValidationError):
            TemplateCreate.model_validate(payload)


def test_generated_json_schema_is_closed_and_uses_public_aliases():
    schemas = controller_schema.contract_json_schemas()
    assert schemas["create"]["additionalProperties"] is False
    assert "deviceTypes" in schemas["create"]["properties"]
    assert "requiredCapabilities" in schemas["create"]["properties"]
    assert schemas["controllerAction"]["additionalProperties"] is False
    assert "event" in schemas["controllerAction"]["required"]
