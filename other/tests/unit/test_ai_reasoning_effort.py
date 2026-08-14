import pytest
from pydantic import ValidationError

from api.models.ai_config import AssistantAIConfigCreate, AssistantAIConfigUpdate


def test_reasoning_effort_accepts_cross_provider_levels():
    for effort in ("", "low", "medium", "high"):
        body = AssistantAIConfigCreate(name="member", reasoning_effort=effort)
        assert body.reasoning_effort == effort


def test_reasoning_effort_rejects_unknown_level():
    with pytest.raises(ValidationError):
        AssistantAIConfigUpdate(reasoning_effort="max")
