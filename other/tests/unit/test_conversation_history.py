from types import SimpleNamespace

from api.services.chat import chat_inject, mcp_session_context
from ai_runtime.inference.conversation_history import build_conversation_history


def _message(role: str, content: str, *, tags: str = "", message_id: int = 1):
    return SimpleNamespace(
        id=message_id,
        role=role,
        content=content,
        tags=tags,
        think="must not be replayed",
    )


def test_filters_runtime_only_rows_and_overrides_latest_user_content():
    history = [
        _message("user", "old request"),
        _message("assistant", "visible reply"),
        _message("system", "failure", tags="system_notice_ai_error"),
        _message("user", "folded", tags="compressed_away"),
        _message("user", "not yet injected", tags=chat_inject.PENDING_INJECT_TAG),
        _message("system", "phase complete", tags="phase_summary"),
    ]

    result = build_conversation_history(
        history,
        system_prompt="system",
        mcp_result_max_chars=200,
        model_user_content="current request",
    )

    assert result == [
        {"role": "system", "content": "system"},
        {"role": "user", "content": "old request"},
        {"role": "assistant", "content": "visible reply"},
        {"role": "user", "content": "current request"},
    ]


def test_reattaches_compacted_mcp_call_to_preceding_assistant(monkeypatch):
    pair = [
        {"role": "assistant", "tool_calls": [{"id": "call-7"}]},
        {"role": "tool", "tool_call_id": "call-7", "content": "result"},
    ]
    monkeypatch.setattr(
        mcp_session_context,
        "compact_mcp_history_messages",
        lambda message_id, content, max_chars: pair,
    )
    history = [
        _message("assistant", "calling"),
        _message("system", "bubble", tags="mcp_tool_call", message_id=7),
    ]

    result = build_conversation_history(
        history,
        system_prompt="system",
        mcp_result_max_chars=123,
    )

    assert result[1] == {
        "role": "assistant",
        "content": "calling",
        "tool_calls": [{"id": "call-7"}],
    }
    assert result[2] == pair[1]


def test_drops_removed_mode_tool_bubble(monkeypatch):
    def fail_if_called(*_args):
        raise AssertionError("removed mode tool must not be compacted")

    monkeypatch.setattr(
        mcp_session_context,
        "compact_mcp_history_messages",
        fail_if_called,
    )

    result = build_conversation_history(
        [_message("system", "mode.manage legacy", tags="mcp_tool_call")],
        system_prompt="system",
        mcp_result_max_chars=100,
    )

    assert result == [{"role": "system", "content": "system"}]
