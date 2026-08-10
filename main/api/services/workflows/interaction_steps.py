"""Pure helpers for the compiled internal AI-intervention step."""

from __future__ import annotations

from typing import Any, Dict


AI_INTERVENTION_TOOL = "__workflow.ai_intervention"


def is_ai_intervention_step(step: Dict[str, Any]) -> bool:
    ref = step.get("toolRef") if isinstance(step.get("toolRef"), dict) else {}
    return step.get("type") == "mcp" and ref.get("name") == AI_INTERVENTION_TOOL
