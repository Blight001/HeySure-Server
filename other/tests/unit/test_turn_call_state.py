import json

from ai_runtime.inference.tool_resolution import (
    TurnCallAction,
    append_pending_call_responses,
    flush_screenshot_messages,
    track_repeated_tool_call,
)


def test_turn_call_actions_are_stable_wire_values():
    assert TurnCallAction.NEXT_CALL.value == "next_call"
    assert TurnCallAction.NEXT_TURN.value == "next_turn"
    assert TurnCallAction.STOP_RUN.value == "stop_run"


def test_repeated_tool_call_tracker_resets_on_signature_change():
    signature, count = track_repeated_tool_call(
        "denied", "workspace.read", {"path": "a"}, "", 0
    )
    assert count == 1

    same_signature, same_count = track_repeated_tool_call(
        "denied", "workspace.read", {"path": "a"}, signature, count
    )
    assert same_signature == signature
    assert same_count == 2

    _, changed_count = track_repeated_tool_call(
        "denied", "workspace.read", {"path": "b"}, same_signature, same_count
    )
    assert changed_count == 1


def test_native_pending_calls_receive_one_tool_response_each():
    conversation = []
    append_pending_call_responses(
        conversation,
        [{"id": "call-1"}, {"id": "call-2"}],
        {"success": False, "error": "barrier"},
        native=True,
    )

    assert [message["tool_call_id"] for message in conversation] == [
        "call-1",
        "call-2",
    ]
    assert all(message["role"] == "tool" for message in conversation)
    assert json.loads(conversation[0]["content"])["error"] == "barrier"


def test_text_pending_calls_are_coalesced_into_one_feedback_message():
    conversation = []
    append_pending_call_responses(
        conversation,
        [{"tool": "workspace.read"}, {"tool": "todo.manage"}],
        {"success": False, "error": "not_executed"},
        native=False,
    )

    assert len(conversation) == 1
    assert "workspace.read, todo.manage" in conversation[0]["content"]
    assert "not_executed" in conversation[0]["content"]


def test_flush_screenshots_preserves_order_and_clears_buffer():
    conversation = [{"role": "tool", "tool_call_id": "call-1"}]
    screenshots = [
        {"role": "user", "content": "image-1"},
        {"role": "user", "content": "image-2"},
    ]

    flush_screenshot_messages(conversation, screenshots)

    assert [item.get("content") for item in conversation[1:]] == [
        "image-1",
        "image-2",
    ]
    assert screenshots == []
