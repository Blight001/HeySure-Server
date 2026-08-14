from types import SimpleNamespace

from ai_runtime.inference import tool_persistence


def test_save_tool_bubble_maps_request_dto_to_chat_message(monkeypatch):
    observed = {}
    fake_session = SimpleNamespace()

    def fake_save_message(session, user_id, message):
        observed["session"] = session
        observed["user_id"] = user_id
        observed["message"] = message
        return SimpleNamespace(
            id=None,
            content=message.content,
            user_id=user_id,
            ai_config_id=message.ai_config_id,
        )

    monkeypatch.setattr(tool_persistence, "_save_message", fake_save_message)
    monkeypatch.setattr(
        tool_persistence,
        "tool_device_identity",
        lambda tool, user_id, result: ("linux-a", "A 服务器"),
    )

    tool_persistence.save_tool_bubble(tool_persistence.ToolBubbleRequest(
        session=fake_session,
        user_id=9,
        ai_config_id=3,
        ai_kind="assistant",
        session_id="session-a",
        session_name="任务",
        model="model-a",
        tool="shell.run",
        arguments={"command": "uptime"},
        result_text='{"success": true}',
        failed=True,
        latency=0.75,
    ))

    message = observed["message"]
    assert observed["session"] is fake_session
    assert observed["user_id"] == 9
    assert message.tags == "mcp_tool_call"
    assert message.latency == 0.75
    assert "设备号: linux-a" in message.content
    assert "状态: 失败" in message.content


def test_record_tool_call_preserves_failure_coordinates(monkeypatch):
    observed = {}

    monkeypatch.setattr(
        "api.services.mcp.mcp_stats.record_call",
        lambda **kwargs: observed.update(kwargs),
    )

    tool_persistence.record_tool_call(tool_persistence.ToolCallRecord(
        tool="workspace.read",
        user_id=9,
        ai_config_id=3,
        session_id="session-a",
        run_id="run-a",
        message_id=17,
        failed=True,
        error="denied",
    ))

    assert observed["success"] is False
    assert observed["error"] == "denied"
    assert observed["session_id"] == "session-a"
    assert observed["run_id"] == "run-a"
    assert observed["message_id"] == 17


def test_screenshot_bubble_url_round_trips():
    content = tool_persistence.build_tool_bubble_content(
        "aifree.browser+screenshot",
        {},
        "ok",
        image_url="https://example.test/screenshot.png",
    )

    assert tool_persistence.extract_screenshot_bubble_url(content) == (
        "https://example.test/screenshot.png"
    )


def test_save_tool_bubble_preserves_result_envelope_for_recording(monkeypatch):
    observed = {}
    fake_session = SimpleNamespace()
    envelope = {
        "success": False,
        "errorCode": "BROWSER_TAKEOVER_REQUIRED",
        "result": {"suggestedAction": "acquire"},
    }

    monkeypatch.setattr(tool_persistence, "_save_message", lambda *_args: SimpleNamespace(
        id=None, content="", user_id=9, ai_config_id=3,
    ))
    monkeypatch.setattr(tool_persistence, "tool_device_identity", lambda *_args: ("", ""))
    monkeypatch.setattr(
        "api.services.workflows.recording_service.record_completed_tool_call",
        lambda _session, call: observed.setdefault("call", call),
    )

    tool_persistence.save_tool_bubble(tool_persistence.ToolBubbleRequest(
        session=fake_session, user_id=9, ai_config_id=3, ai_kind="assistant",
        session_id="session-a", session_name="Task", model="model-a",
        tool="browser.replace", arguments={}, result_text="takeover required",
        tool_result=envelope,
    ))

    assert observed["call"].success is True
    assert observed["call"].result == envelope
