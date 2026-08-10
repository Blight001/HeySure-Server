"""Per-step device ownership, availability, contract and permission checks."""

from __future__ import annotations

import json
from typing import Any, Dict, List

from jsonschema import Draft202012Validator
from sqlmodel import Session, select

from api.devices.mcp_permissions import get_scope
from api.core.settings import settings
from api.models import DevicePresence, User, WorkflowCard, WorkflowCardVersion
from api.services.device_tools.device_permission_policy import get_policy

from .compiler import schema_digest
from .interaction_steps import is_ai_intervention_step


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


def _load_json(raw: str, fallback: Any) -> Any:
    try:
        return json.loads(raw or "")
    except Exception:
        return fallback


def _bound_device_ids(version: WorkflowCardVersion, contracts: Dict[str, Any]) -> List[str]:
    values = _load_json(version.contract_device_ids_json, [])
    if isinstance(values, list) and values:
        return [str(item).strip() for item in values if str(item).strip()]
    legacy = []
    for contract in contracts.values():
        if not isinstance(contract, dict):
            continue
        published = contract.get("publishedDeviceIds")
        if isinstance(published, list):
            legacy.extend(str(item).strip() for item in published if str(item).strip())
        elif str(contract.get("publishedDeviceId") or "").strip():
            legacy.append(str(contract["publishedDeviceId"]).strip())
    return list(dict.fromkeys(legacy))


def _required_tools(definition: Dict[str, Any]) -> List[str]:
    names = []
    for step in definition.get("steps", {}).values():
        if not isinstance(step, dict) or step.get("type") != "mcp" or is_ai_intervention_step(step):
            continue
        name = str(step.get("toolRef", {}).get("name") or "").strip()
        if name:
            names.append(name)
    return sorted(set(names))


def _validate_live_contract(
    device: DevicePresence,
    name: str,
    live: Any,
    contract: Dict[str, Any],
) -> None:
    if not isinstance(live, dict):
        raise WorkflowDispatchError("TOOL_NOT_AVAILABLE", f"tool is not currently reported: {name}")
    expected = str(contract.get("schemaDigest") or "")
    current_schema = live.get("input_schema") if isinstance(live.get("input_schema"), dict) else {}
    if expected and schema_digest(current_schema) != expected:
        raise WorkflowDispatchError("TOOL_SCHEMA_INCOMPATIBLE", f"tool schema changed after publication: {name}")
    providers = contract.get("providers")
    device_type = str(device.device_type or "custom")
    if isinstance(providers, list) and providers and device_type not in providers:
        raise WorkflowDispatchError("TOOL_SCHEMA_INCOMPATIBLE", f"tool provider is incompatible: {name}")


def validate_run_device(
    session: Session,
    *,
    user_id: int,
    device_id: str,
    definition: Dict[str, Any],
    version: WorkflowCardVersion,
) -> DevicePresence:
    """Fail before run creation unless the selected endpoint can execute the release."""
    device = session.exec(select(DevicePresence).where(
        DevicePresence.user_id == user_id,
        DevicePresence.device_id == device_id,
    )).first()
    if not device:
        raise WorkflowDispatchError("DEVICE_ACCESS_DENIED", "device is not owned by the run user")
    if not device.online:
        raise WorkflowDispatchError("DEVICE_OFFLINE", "device is offline", retryable=True)
    contracts = _load_json(version.tool_contracts_json, {})
    contracts = contracts if isinstance(contracts, dict) else {}
    bound_ids = _bound_device_ids(version, contracts)
    if bound_ids and device_id not in bound_ids:
        raise WorkflowDispatchError("DEVICE_NOT_BOUND_TO_CARD", "device is not bound to this card version")
    live_defs = _tool_defs(device)
    for name in _required_tools(definition):
        contract = contracts.get(name) if isinstance(contracts.get(name), dict) else {}
        _validate_live_contract(device, name, live_defs.get(name), contract)
    return device


def validate_step_dispatch(
    session: Session,
    *,
    user_id: int,
    device_id: str,
    tool_name: str,
    expected_provider: str,
    expected_digest: str,
    arguments: Dict[str, Any],
    confirmation_granted: bool = False,
    card_id: str = "",
    card_version_id: str = "",
) -> DevicePresence:
    if not session.get(User, user_id):
        raise WorkflowDispatchError("DEVICE_ACCESS_DENIED", "run user no longer exists")
    if card_id or card_version_id:
        card = session.get(WorkflowCard, card_id)
        version = session.get(WorkflowCardVersion, card_version_id)
        if (
            not card or card.user_id != user_id or card.status not in {"published", "deprecated"}
            or not version or version.card_id != card.id
        ):
            raise WorkflowDispatchError("CARD_VERSION_NOT_RUNNABLE", "card version is no longer runnable")
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
    if expected_provider and str(device.device_type or "custom") != expected_provider:
        raise WorkflowDispatchError(
            "TOOL_SCHEMA_INCOMPATIBLE",
            f"tool provider requires device type {expected_provider}, got {device.device_type or 'custom'}",
        )
    definition = _tool_defs(device).get(tool_name)
    if not isinstance(definition, dict):
        raise WorkflowDispatchError("TOOL_NOT_AVAILABLE", f"tool is not currently reported: {tool_name}")
    scope = get_scope(user_id, device_id)
    if scope is None or tool_name not in scope:
        raise WorkflowDispatchError("TOOL_PERMISSION_DENIED", f"tool is not allowed on device: {tool_name}")
    policy = get_policy(user_id, str(device.device_type or "custom"))
    denied_tags = [
        str(tag)
        for tag in definition.get("permissions", [])
        if policy.get(str(tag)) == "deny"
    ]
    if denied_tags:
        raise WorkflowDispatchError(
            "TOOL_PERMISSION_DENIED",
            "device permission policy denies: " + ", ".join(sorted(denied_tags)),
        )
    current_schema = definition.get("input_schema")
    current_schema = current_schema if isinstance(current_schema, dict) else {}
    if expected_digest and schema_digest(current_schema) != expected_digest:
        raise WorkflowDispatchError("TOOL_SCHEMA_INCOMPATIBLE", "tool input schema changed after card publication")
    if bool(definition.get("destructive")) and not confirmation_granted:
        raise WorkflowDispatchError(
            "CONFIRMATION_REQUIRED",
            "destructive tools require an approved workflow confirmation",
        )
    if len(json.dumps(arguments, ensure_ascii=False, separators=(",", ":"), default=str).encode("utf-8")) > int(
        settings.workflow_max_argument_bytes
    ):
        raise WorkflowDispatchError("ARGUMENT_VALIDATION_FAILED", "rendered arguments exceed size limit")
    errors = sorted(Draft202012Validator(current_schema).iter_errors(arguments), key=lambda item: list(item.path))
    if errors:
        message = errors[0].message
        raise WorkflowDispatchError("ARGUMENT_VALIDATION_FAILED", message)
    return device
