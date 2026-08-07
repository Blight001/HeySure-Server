"""Per-step device ownership, availability, contract and permission checks."""

from __future__ import annotations

import json
from typing import Any, Dict

from jsonschema import Draft202012Validator
from sqlmodel import Session, select

from api.devices.mcp_permissions import get_scope
from api.models import DevicePresence

from .compiler import schema_digest


class WorkflowDispatchError(RuntimeError):
    def __init__(self, code: str, message: str, *, retryable: bool = False):
        self.code = code
        self.retryable = retryable
        super().__init__(message)


def _tool_defs(row: DevicePresence) -> Dict[str, dict]:
    try:
        value = json.loads(row.tool_defs_json or "{}")
    except Exception:
        value = {}
    return value if isinstance(value, dict) else {}


def validate_step_dispatch(
    session: Session,
    *,
    user_id: int,
    device_id: str,
    tool_name: str,
    expected_digest: str,
    arguments: Dict[str, Any],
) -> DevicePresence:
    device = session.exec(
        select(DevicePresence).where(
            DevicePresence.user_id == user_id,
            DevicePresence.device_id == device_id,
        )
    ).first()
    if not device:
        raise WorkflowDispatchError("DEVICE_ACCESS_DENIED", "device is not owned by the run user")
    if not device.online:
        raise WorkflowDispatchError("DEVICE_OFFLINE", "device is offline", retryable=True)
    definition = _tool_defs(device).get(tool_name)
    if not isinstance(definition, dict):
        raise WorkflowDispatchError("TOOL_NOT_AVAILABLE", f"tool is not currently reported: {tool_name}")
    scope = get_scope(user_id, device_id)
    if scope is None or tool_name not in scope:
        raise WorkflowDispatchError("TOOL_PERMISSION_DENIED", f"tool is not allowed on device: {tool_name}")
    current_schema = definition.get("input_schema")
    current_schema = current_schema if isinstance(current_schema, dict) else {}
    if expected_digest and schema_digest(current_schema) != expected_digest:
        raise WorkflowDispatchError("TOOL_SCHEMA_INCOMPATIBLE", "tool input schema changed after card publication")
    if bool(definition.get("destructive")):
        raise WorkflowDispatchError(
            "CONFIRMATION_REQUIRED",
            "destructive tools require the phase-three confirmation workflow",
        )
    errors = sorted(Draft202012Validator(current_schema).iter_errors(arguments), key=lambda item: list(item.path))
    if errors:
        message = errors[0].message
        raise WorkflowDispatchError("ARGUMENT_VALIDATION_FAILED", message)
    return device
