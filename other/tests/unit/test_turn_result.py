import json
from types import SimpleNamespace

from ai_runtime.inference import turn_result


def _context(conversation=None):
    return turn_result.AssistantTurnContext(
        session=SimpleNamespace(),
        conversation=conversation if conversation is not None else [],
        user_id=7,
        ai_config_id=3,
        ai_kind="assistant",
        session_id="session-a",
        session_name="任务",
        model="model-a",
        system_prompt="system",
        native_tool_name_map={"native_search": "web.search"},
        allowed_tools=frozenset({"web.search"}),
    )


def test_native_turn_is_resolved_persisted_and_appended(monkeypatch):
    saved_messages = []
    saved = SimpleNamespace(id=11)
    monkeypatch.setattr(
        turn_result,
        "_save_message",
        lambda session, user_id, message: saved_messages.append(message) or saved,
    )
    context = _context([{"role": "user", "content": "search"}])
    stream = SimpleNamespace(
        assistant_text="",
        reasoning_content="thinking",
        usage={"prompt_tokens": 10, "completion_tokens": 4, "total_tokens": 14},
        finish_reason="tool_calls",
        has_native_tc=True,
        tool_calls=[{
            "id": "call-1",
            "native_name": "native_search",
            "tool": "native_search",
            "arguments": {"q": "HeySure"},
            "raw_arguments": "",
        }],
    )

    result = turn_result.persist_assistant_turn(context, stream, 0.25)

    assert result.saved_message is saved
    assert result.conversation_start == 1
    assert result.token_triplet == "10/4/14"
    assert result.tool_calls[0]["tool"] == "web.search"
    assert saved_messages[0].tags == "mcp_assistant_call"
    assert saved_messages[0].latency == 0.25
    appended = context.conversation[-1]
    assert appended["content"] is None
    assert appended["reasoning_content"] == "thinking"
    function = appended["tool_calls"][0]["function"]
    assert function["name"] == "native_search"
    assert json.loads(function["arguments"]) == {"q": "HeySure"}


def test_text_turn_preserves_assistant_content_and_empty_usage(monkeypatch):
    saved_messages = []
    monkeypatch.setattr(
        turn_result,
        "_save_message",
        lambda session, user_id, message: saved_messages.append(message) or SimpleNamespace(id=12),
    )
    context = _context()
    stream = SimpleNamespace(
        assistant_text="done",
        reasoning_content="",
        usage={},
        finish_reason="stop",
        has_native_tc=False,
        tool_calls=[],
    )

    result = turn_result.persist_assistant_turn(context, stream, 0.1)

    assert result.tool_calls == []
    assert result.token_triplet == "0/0/0"
    assert saved_messages[0].tags == ""
    assert context.conversation == [{"role": "assistant", "content": "done"}]
