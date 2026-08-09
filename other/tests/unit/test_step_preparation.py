from types import SimpleNamespace

from ai_runtime.inference import step_preparation


def _message_context(ai_config_id=3):
    return step_preparation.StepMessageContext(
        session=SimpleNamespace(),
        conversation=[],
        user_id=9,
        ai_config_id=ai_config_id,
        ai_kind="assistant",
        session_id="session-a",
        session_name="任务",
        model="model-a",
    )


def test_step_ingest_appends_user_inject_without_ai_config(monkeypatch):
    context = _message_context(ai_config_id=None)
    monkeypatch.setattr(
        step_preparation.chat_inject,
        "pop_pending_injects",
        lambda *args: ["new user message"],
    )

    pending = step_preparation.ingest_step_messages(context, "existing-reply")

    assert pending == "existing-reply"
    assert context.conversation == [
        {"role": "user", "content": "new user message"}
    ]


def test_inbound_inquiry_is_persisted_and_requests_reply(monkeypatch):
    context = _message_context()
    saved = []
    inbound = SimpleNamespace(
        from_ai_config_id=4,
        message_id="message-a",
        content="status?",
        message_type="inquiry",
        require_reply=True,
    )
    monkeypatch.setattr(
        step_preparation.ai_message_service,
        "pop_pending_for",
        lambda *args: inbound,
    )
    monkeypatch.setattr(step_preparation, "_resolve_ai_name", lambda session, value: f"AI-{value}")
    monkeypatch.setattr(
        step_preparation,
        "_save_message",
        lambda session, user_id, message: saved.append(message),
    )
    monkeypatch.setattr(
        step_preparation.chat_inject,
        "pop_pending_injects",
        lambda *args: [],
    )

    pending = step_preparation.ingest_step_messages(context, "")

    assert pending == "message-a"
    assert saved[0].tags == "ai_message_inbound:inquiry:message-a"
    assert context.conversation[0]["role"] == "user"
    assert "status?" in context.conversation[0]["content"]


def test_inbox_poll_failure_preserves_pending_reply(monkeypatch):
    context = _message_context()
    monkeypatch.setattr(
        step_preparation.ai_message_service,
        "pop_pending_for",
        lambda *args: (_ for _ in ()).throw(RuntimeError("offline")),
    )
    monkeypatch.setattr(
        step_preparation.chat_inject,
        "pop_pending_injects",
        lambda *args: [],
    )

    assert step_preparation.ingest_step_messages(context, "existing") == "existing"


def test_inactive_mcp_exposes_no_provider_tools():
    exposure = step_preparation.select_tool_exposure(
        step_preparation.ToolExposureRequest(
            mcp_active=False,
            exposed_tools=frozenset({"workspace.read"}),
            allowed_tools=frozenset({"workspace.read"}),
            task_runtime=False,
            plan_active=False,
            awaiting_finish=False,
            tool_protocol="native",
        )
    )

    assert exposure.current_tools == frozenset()
    assert exposure.provider_tools == []
    assert exposure.native_name_map == {}


def test_task_preplan_text_protocol_uses_narrow_prompt_only_surface():
    allowed = frozenset({
        "todo.manage",
        "mcp.describe+tool",
        "knowledge.search",
        "workspace.write",
    })

    exposure = step_preparation.select_tool_exposure(
        step_preparation.ToolExposureRequest(
            mcp_active=True,
            exposed_tools=allowed,
            allowed_tools=allowed,
            task_runtime=True,
            plan_active=False,
            awaiting_finish=False,
            tool_protocol="text",
        )
    )

    assert "todo.manage" in exposure.current_tools
    assert "knowledge.search" in exposure.current_tools
    assert "workspace.write" not in exposure.current_tools
    assert exposure.provider_tools == []


def test_native_exposure_builds_provider_payload(monkeypatch):
    monkeypatch.setattr(
        step_preparation,
        "build_native_tools_payload",
        lambda allowed: ([{"name": "schema"}], {"workspace_read": "workspace.read"}),
    )
    exposure = step_preparation.select_tool_exposure(
        step_preparation.ToolExposureRequest(
            mcp_active=True,
            exposed_tools=frozenset({"workspace.read"}),
            allowed_tools=frozenset({"workspace.read"}),
            task_runtime=False,
            plan_active=False,
            awaiting_finish=False,
            tool_protocol="native",
        )
    )

    assert exposure.provider_tools == [{"name": "schema"}]
    assert exposure.native_name_map == {"workspace_read": "workspace.read"}
