import json

from api.chat_runtime import chat_stream


class _Response:
    def __init__(self, payload):
        self.payload = payload
        self.closed = False

    def iter_lines(self):
        yield json.dumps(self.payload).encode("utf-8")

    def close(self):
        self.closed = True


def _patch_live_state(monkeypatch, usage_callback=lambda *_: None):
    monkeypatch.setattr(chat_stream, "_set_run_live_text", lambda *_: None)
    monkeypatch.setattr(chat_stream, "_set_run_live_reasoning", lambda *_: None)
    monkeypatch.setattr(chat_stream, "_set_run_live_phase", lambda *_: None)
    monkeypatch.setattr(chat_stream, "_set_run_live_usage", usage_callback)
    monkeypatch.setattr(chat_stream, "_run_should_stop", lambda *_: False)


def test_openai_compat_accepts_buffered_json_and_usage(monkeypatch):
    live_usage = []
    _patch_live_state(monkeypatch, lambda *args: live_usage.append(args[1:]))
    response = _Response({
        "choices": [{
            "message": {"role": "assistant", "content": "一次性完整回答"},
            "finish_reason": "stop",
        }],
        "usage": {"prompt_tokens": 21, "completion_tokens": 8, "total_tokens": 29},
    })

    result = chat_stream.stream_turn_openai_compat("run-1", response, {})

    assert result.assistant_text == "一次性完整回答"
    assert result.usage == {"prompt_tokens": 21, "completion_tokens": 8, "total_tokens": 29}
    assert result.finish_reason == "stop"
    assert (21, 8, 29) in live_usage
    assert response.closed is True


def test_openai_compat_accepts_buffered_native_tool_calls(monkeypatch):
    _patch_live_state(monkeypatch)
    response = _Response({
        "choices": [{"message": {"tool_calls": [{
            "id": "call_real",
            "type": "function",
            "function": {"name": "todo_manage", "arguments": "{\"action\":\"list\"}"},
        }]}, "finish_reason": "tool_calls"}],
        "usage": {"prompt_tokens": 10, "completion_tokens": 3, "total_tokens": 13},
    })

    result = chat_stream.stream_turn_openai_compat(
        "run-2", response, {"todo_manage": "todo.manage"}
    )

    assert result.has_native_tc is True
    assert result.tool_calls[0]["id"] == "call_real"
    assert result.tool_calls[0]["tool"] == "todo.manage"
    assert result.tool_calls[0]["arguments"] == {"action": "list"}


def test_openai_compat_repairs_inconsistent_provider_total(monkeypatch):
    live_usage = []
    _patch_live_state(monkeypatch, lambda *args: live_usage.append(args[1:]))
    response = _Response({
        "choices": [{
            "message": {"role": "assistant", "content": "ok"},
            "finish_reason": "stop",
        }],
        "usage": {"prompt_tokens": 100, "completion_tokens": 9, "total_tokens": 40},
    })

    result = chat_stream.stream_turn_openai_compat("run-bad-total", response, {})

    assert result.usage["total_tokens"] == 109
    assert (100, 9, 109) in live_usage


def test_anthropic_usage_includes_cached_input_in_prompt_total():
    usage = chat_stream._anthropic_input_usage({
        "input_tokens": 12,
        "cache_read_input_tokens": 80,
        "cache_creation_input_tokens": 6,
    })
    usage = chat_stream._anthropic_final_usage(usage, {"output_tokens": 7})

    assert usage["prompt_tokens"] == 98
    assert usage["completion_tokens"] == 7
    assert usage["total_tokens"] == 105
    assert usage["cache_read_input_tokens"] == 80
