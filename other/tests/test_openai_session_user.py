"""Stable anonymous session IDs let local CLI gateways resume provider state."""

from ai_runtime.inference.core import _heysure_provider_session_id


def test_openai_session_user_is_stable_and_anonymous():
    first = _heysure_provider_session_id(7, 11, "assistant", "session-private-name")
    second = _heysure_provider_session_id(7, 11, "assistant", "session-private-name")

    assert first == second
    assert first.startswith("heysure-")
    assert len(first) == len("heysure-") + 64
    assert "session-private-name" not in first


def test_openai_session_user_changes_with_conversation_identity():
    base = _heysure_provider_session_id(7, 11, "assistant", "session-a")

    assert base != _heysure_provider_session_id(7, 11, "assistant", "session-b")
    assert base != _heysure_provider_session_id(8, 11, "assistant", "session-a")
    assert base != _heysure_provider_session_id(7, 12, "assistant", "session-a")
