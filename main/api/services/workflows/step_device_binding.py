"""Resolve the device and frozen contract assigned to one workflow MCP node."""

from __future__ import annotations

import json
from typing import Any, Dict

from sqlmodel import Session

from api.models import WorkflowCardVersion, WorkflowRun, WorkflowStepRun


def step_device_id(step: Dict[str, Any], run: WorkflowRun) -> str:
    """Use a node binding when present and retain the run device for legacy cards."""
    ref = step.get("toolRef") if isinstance(step.get("toolRef"), dict) else {}
    return str(ref.get("deviceId") or run.device_id).strip()


def step_contract(definition: Dict[str, Any], step_run: WorkflowStepRun) -> Dict[str, Any]:
    """Prefer new per-step contracts, falling back to legacy tool-name contracts."""
    contracts = definition.get("_toolContracts", {})
    contract = contracts.get(step_run.step_id) if isinstance(contracts, dict) else None
    if not isinstance(contract, dict) and isinstance(contracts, dict):
        contract = contracts.get(step_run.tool_name)
    return contract if isinstance(contract, dict) else {}


def step_run_device_id(session: Session, step_run: WorkflowStepRun) -> str:
    run = session.get(WorkflowRun, step_run.run_id)
    version = session.get(WorkflowCardVersion, run.card_version_id) if run else None
    try:
        definition = json.loads(version.definition_json or "{}") if version else {}
    except Exception:
        definition = {}
    step = definition.get("steps", {}).get(step_run.step_id, {})
    if not run or not isinstance(step, dict):
        raise ValueError("workflow run step is missing")
    return step_device_id(step, run)
