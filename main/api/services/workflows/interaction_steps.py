"""Pure helpers for first-class AI review workflow steps."""

from __future__ import annotations

from typing import Any, Dict


def is_ai_review_step(step: Dict[str, Any]) -> bool:
    return step.get("type") == "ai"
