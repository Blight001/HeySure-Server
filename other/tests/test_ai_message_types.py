import pytest
from fastapi import HTTPException

from ai_runtime.inference.ai_message_service import _normalize_message_type
from ai_runtime.inference.communication_prompt import normalize_ai_message_type
from mcp_runtime.mcp import registry as _registry  # noqa: F401 - initialize tools
from tools.communication import _coerce_message_type


@pytest.mark.parametrize("message_type", ["inquiry", "reply", "notify"])
def test_supported_ai_message_types(message_type):
    assert _coerce_message_type(message_type) == message_type
    assert _normalize_message_type(message_type, require_reply=False) == message_type
    assert normalize_ai_message_type(message_type, False) == message_type


def test_chitchat_is_rejected_by_public_tool_contract():
    with pytest.raises(HTTPException) as exc:
        _coerce_message_type("chitchat")
    assert exc.value.status_code == 400
    assert "chitchat" not in str(exc.value.detail)


def test_legacy_chitchat_normalizes_to_existing_fallback_semantics():
    # Old persisted rows remain readable during rolling deploys, but no new
    # chitchat message can enter through the public tool contract.
    assert _normalize_message_type("chitchat", require_reply=False) == "notify"
    assert _normalize_message_type("chitchat", require_reply=True) == "inquiry"
    assert normalize_ai_message_type("chitchat", False) == "notify"
    assert normalize_ai_message_type("chitchat", True) == "inquiry"
