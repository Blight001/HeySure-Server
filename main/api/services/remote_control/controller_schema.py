"""Strict ``remote_controller_template.v1`` and P2P action contracts."""

from __future__ import annotations

import json
from typing import Annotated, Any, Literal, Optional, Union

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


SCHEMA_NAME = "remote_controller_template.v1"
MAX_TEMPLATES_PER_USER = 32
MAX_TEMPLATE_BYTES = 64 * 1024
MAX_CONTROLS = 64
MAX_COLUMNS = 12

TemplateId = Annotated[str, Field(pattern=r"^[a-z][a-z0-9_.-]{0,63}$")]
ControlId = Annotated[str, Field(pattern=r"^[a-z][a-z0-9_.-]{0,63}$")]
DeviceType = Literal["desktop", "android", "browser"]
Capability = Literal["remote_control", "remote.control", "remote_controller_templates"]
ControlKind = Literal["button", "dpad", "keypad", "slider", "joystick", "textInput"]
Tone = Literal["default", "primary", "danger"]
Gap = Literal["xs", "sm", "md", "lg"]
AllowedKey = Literal[
    "ArrowUp", "ArrowDown", "ArrowLeft", "ArrowRight", "Enter", "Escape",
    "Home", "End", "PageUp", "PageDown", "Tab", "Space", "Backspace", "Delete",
    "F1", "F2", "F3", "F4", "F5", "F6", "F7", "F8", "F9", "F10", "F11", "F12",
    "MediaPlayPause", "MediaTrackPrevious", "MediaTrackNext",
    "AudioVolumeDown", "AudioVolumeUp", "AudioVolumeMute",
]
_RESERVED_EVENT_PREFIXES = ("rc.", "rc-", "rt.", "rt-", "web-action", "controller-action")


def validate_logical_event(value: str) -> str:
    if value.startswith(_RESERVED_EVENT_PREFIXES):
        raise ValueError("event uses a reserved transport prefix")
    return value


class StrictModel(BaseModel):
    model_config = ConfigDict(
        extra="forbid",
        populate_by_name=False,
        str_strip_whitespace=True,
        allow_inf_nan=False,
        strict=True,
    )


class KeyAction(StrictModel):
    type: Literal["key"]
    key: AllowedKey


class BrowserAction(StrictModel):
    type: Literal["browser"]
    action: Literal["back", "forward", "reload"]


class EmitAction(StrictModel):
    type: Literal["emit"]
    event: str = Field(pattern=r"^[a-z][a-z0-9_.-]{0,63}$")

    @field_validator("event")
    @classmethod
    def reject_reserved_transport_events(cls, value: str) -> str:
        return validate_logical_event(value)


ControllerAction = Annotated[
    Union[KeyAction, BrowserAction, EmitAction],
    Field(discriminator="type"),
]


class ControllerLayout(StrictModel):
    columns: int = Field(default=3, ge=1, le=MAX_COLUMNS)
    gap: Gap = "sm"


class ControllerControl(StrictModel):
    id: ControlId
    kind: ControlKind
    label: str = Field(min_length=1, max_length=40)
    tone: Tone = "default"
    action: ControllerAction
    minimum: Optional[float] = Field(default=None, alias="min", ge=-1_000_000, le=1_000_000)
    maximum: Optional[float] = Field(default=None, alias="max", ge=-1_000_000, le=1_000_000)
    step: Optional[float] = Field(default=None, gt=0)
    dead_zone: Optional[float] = Field(default=None, alias="deadZone", ge=0, le=0.95)
    max_length: Optional[int] = Field(default=None, alias="maxLength", ge=1, le=1024)

    @model_validator(mode="after")
    def validate_kind_options(self):
        self._validate_range_options()
        self._validate_text_options()
        self._validate_action_type()
        return self

    def _validate_range_options(self) -> None:
        ranged = self.minimum is not None or self.maximum is not None or self.step is not None
        if self.kind == "slider":
            if self.minimum is None or self.maximum is None or self.step is None:
                raise ValueError("slider requires min, max, and step")
            if self.minimum >= self.maximum or self.step > self.maximum - self.minimum:
                raise ValueError("slider range is invalid")
        elif ranged:
            raise ValueError("min, max, and step are only valid for slider")
        if self.kind != "joystick" and self.dead_zone is not None:
            raise ValueError("deadZone is only valid for joystick")

    def _validate_text_options(self) -> None:
        if self.kind == "textInput" and self.max_length is None:
            raise ValueError("textInput requires maxLength")
        if self.kind != "textInput" and self.max_length is not None:
            raise ValueError("maxLength is only valid for textInput")

    def _validate_action_type(self) -> None:
        if self.kind != "button" and not isinstance(self.action, EmitAction):
            raise ValueError("non-button controls require an emit action")


class TemplateContent(StrictModel):
    schema_name: Literal[SCHEMA_NAME] = Field(default=SCHEMA_NAME, alias="schema")
    name: str = Field(min_length=1, max_length=80)
    device_types: list[DeviceType] = Field(alias="deviceTypes", min_length=1, max_length=3)
    required_capabilities: list[Capability] = Field(
        alias="requiredCapabilities",
        min_length=1,
        max_length=3,
    )
    layout: ControllerLayout
    controls: list[ControllerControl] = Field(min_length=1, max_length=MAX_CONTROLS)

    @field_validator("device_types", "required_capabilities")
    @classmethod
    def require_unique_values(cls, value: list[str]) -> list[str]:
        if len(value) != len(set(value)):
            raise ValueError("values must be unique")
        return value

    @model_validator(mode="after")
    def validate_template(self):
        if not {"remote_control", "remote.control"}.intersection(self.required_capabilities):
            raise ValueError("requiredCapabilities must include remote_control")
        if any(isinstance(item.action, EmitAction) for item in self.controls):
            if "remote_controller_templates" not in self.required_capabilities:
                raise ValueError("emit actions require remote_controller_templates capability")
        control_ids = [item.id for item in self.controls]
        if len(control_ids) != len(set(control_ids)):
            raise ValueError("control ids must be unique")
        encoded = json.dumps(
            self.model_dump(mode="json", by_alias=True),
            ensure_ascii=False,
            separators=(",", ":"),
        ).encode("utf-8")
        if len(encoded) > MAX_TEMPLATE_BYTES:
            raise ValueError(f"template exceeds {MAX_TEMPLATE_BYTES} bytes")
        return self


class TemplateCreate(TemplateContent):
    id: TemplateId


class TemplateUpdate(TemplateContent):
    expected_revision: int = Field(alias="expectedRevision", ge=1)


class TemplateDocument(TemplateContent):
    id: TemplateId
    revision: int = Field(ge=1)
    builtin: bool = False


class RestoreRequest(StrictModel):
    expected_revision: int = Field(alias="expectedRevision", ge=1)


class AxisValue(StrictModel):
    x: float = Field(ge=-1, le=1)
    y: float = Field(ge=-1, le=1)


ControllerText = Annotated[str, Field(max_length=1024)]
ControllerNumber = Annotated[float, Field(ge=-1_000_000, le=1_000_000)]
ControllerValue = Union[None, ControllerNumber, ControllerText, AxisValue]


class ControllerActionMessage(StrictModel):
    kind: Literal["controller-action"]
    v: Literal[1]
    template_id: TemplateId = Field(alias="templateId")
    control_id: ControlId = Field(alias="controlId")
    seq: int = Field(ge=0, le=2**53 - 1)
    phase: Literal["trigger", "start", "update", "end"]
    event: str = Field(pattern=r"^[a-z][a-z0-9_.-]{0,63}$")
    value: ControllerValue = None
    ts: int = Field(ge=0, le=2**53 - 1)

    @field_validator("event")
    @classmethod
    def reject_reserved_event(cls, value: str) -> str:
        return validate_logical_event(value)


def contract_json_schemas() -> dict[str, Any]:
    return {
        "template": TemplateDocument.model_json_schema(by_alias=True),
        "create": TemplateCreate.model_json_schema(by_alias=True),
        "update": TemplateUpdate.model_json_schema(by_alias=True),
        "controllerAction": ControllerActionMessage.model_json_schema(by_alias=True),
    }
