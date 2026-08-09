from types import SimpleNamespace

from ai_runtime.inference import model_error_flow


def _context(conversation=None):
    phases = []
    errors = []
    saved = []
    context = model_error_flow.ModelErrorContext(
        session=SimpleNamespace(),
        conversation=conversation or [],
        user_id=9,
        ai_config_id=3,
        ai_kind="assistant",
        session_id="session-a",
        session_name="任务",
        model="model-a",
        set_generating=lambda: phases.append("generating"),
        set_run_error=errors.append,
    )
    return context, phases, errors, saved


def test_missing_tool_response_is_repaired_and_error_count_resets(monkeypatch):
    conversation = [{
        "role": "assistant",
        "tool_calls": [{"id": "call-1"}],
    }]
    context, phases, errors, saved = _context(conversation)
    monkeypatch.setattr(
        model_error_flow,
        "_save_message",
        lambda session, user_id, message: saved.append(message),
    )

    decision = model_error_flow.handle_model_error(
        context,
        "invalid tool context",
        1,
        False,
    )

    assert decision.consecutive_errors == 0
    assert decision.stop_run is False
    assert conversation[1]["tool_call_id"] == "call-1"
    assert saved[0].tags == "system_notice_ai_context_repaired"
    assert phases == ["generating"]
    assert errors == []


def test_image_unsupported_error_degrades_images_and_continues(monkeypatch):
    conversation = [{
        "role": "user",
        "content": [
            {"type": "text", "text": "look"},
            {"type": "image_url", "image_url": {"url": "data:image/png;base64,x"}},
        ],
    }]
    context, _, _, saved = _context(conversation)
    monkeypatch.setattr(
        model_error_flow,
        "_save_message",
        lambda session, user_id, message: saved.append(message),
    )

    decision = model_error_flow.handle_model_error(
        context,
        "image_url is unsupported for this text-only model",
        2,
        False,
    )

    assert decision.consecutive_errors == 0
    assert decision.image_input_disabled is True
    assert all(
        block.get("type") != "image_url"
        for block in conversation[0]["content"]
    )
    assert conversation[-1]["role"] == "user"
    assert saved[0].tags == "system_notice_ai_error"


def test_third_plain_model_error_stops_run(monkeypatch):
    context, phases, errors, saved = _context()
    monkeypatch.setattr(
        model_error_flow,
        "_save_message",
        lambda session, user_id, message: saved.append(message),
    )

    decision = model_error_flow.handle_model_error(
        context,
        "upstream unavailable",
        2,
        False,
    )

    assert decision.consecutive_errors == 3
    assert decision.stop_run is True
    assert errors == [
        "AI request failed 3 times consecutively: upstream unavailable"
    ]
    assert phases == ["generating"]
    assert "连续错误次数: 3/3" in saved[0].content


def test_first_plain_error_is_persisted_without_user_injection(monkeypatch):
    context, _, _, saved = _context()
    monkeypatch.setattr(
        model_error_flow,
        "_save_message",
        lambda session, user_id, message: saved.append(message),
    )

    decision = model_error_flow.handle_model_error(
        context,
        "temporary",
        0,
        False,
    )

    assert decision.consecutive_errors == 1
    assert context.conversation == []
    assert "系统将重试上游请求" in saved[0].content
