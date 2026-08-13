from types import SimpleNamespace

from ai_runtime.inference import tool_rejections
from ai_runtime.inference.tool_resolution import TurnCallAction


def _context(*, native=True):
    phases = []
    errors = []
    session = SimpleNamespace(add=lambda row: None, commit=lambda: None)
    saved = SimpleNamespace(id=17, tags="")
    return tool_rejections.RejectionContext(
        session=session,
        conversation=[],
        pending=[{"id": "call-2", "tool": "workspace.read"}],
        saved_message=saved,
        user_id=9,
        ai_config_id=3,
        ai_kind="assistant",
        session_id="session-a",
        session_name="任务",
        model="model-a",
        run_id="run-a",
        native_tool_calls=native,
        set_live_phase=phases.append,
        set_run_error=errors.append,
    ), phases, errors


def test_mcp_disabled_native_feedback_preserves_tool_only_protocol(monkeypatch):
    context, phases, errors = _context(native=True)
    saved_messages = []
    monkeypatch.setattr(
        tool_rejections,
        "_save_message",
        lambda session, user_id, message: saved_messages.append(message),
    )

    outcome = tool_rejections.handle_mcp_disabled(
        context,
        "workspace.read",
        {"path": "README.md"},
        "call-1",
        "",
        0,
    )

    assert outcome.action is TurnCallAction.NEXT_TURN
    assert [item["role"] for item in context.conversation] == ["tool", "tool"]
    assert [item["tool_call_id"] for item in context.conversation] == [
        "call-1",
        "call-2",
    ]
    assert saved_messages[0].role == "user"
    assert saved_messages[0].tags == "system_notice_mcp_disabled"
    assert phases == ["generating"]
    assert errors == []


def test_mcp_disabled_third_repeat_stops_run(monkeypatch):
    context, _, errors = _context(native=False)
    monkeypatch.setattr(tool_rejections, "_save_message", lambda *args: None)

    outcome = tool_rejections.handle_mcp_disabled(
        context,
        "workspace.read",
        {},
        "call-1",
        "mcp_disabled|workspace.read|{}",
        2,
    )

    assert outcome.action is TurnCallAction.STOP_RUN
    assert outcome.count == 3
    assert errors == ["Repeated MCP call while MCP is disabled"]


def test_disallowed_tool_persists_failure_and_returns_next_call(monkeypatch):
    context, _, errors = _context(native=False)
    bubbles = []
    monkeypatch.setattr(
        tool_rejections.tool_persistence,
        "save_tool_bubble",
        bubbles.append,
    )

    outcome = tool_rejections.handle_disallowed_tool(
        context,
        "workspace.write",
        {"path": "x"},
        "call-1",
        {"workspace.read"},
        "",
        0,
    )

    assert outcome.action is TurnCallAction.NEXT_CALL
    assert "workspace.write" in context.conversation[0]["content"]
    assert "workspace.read" in context.conversation[0]["content"]
    assert bubbles[0].failed is True
    assert "workspace.write" in context.saved_message.tags
    assert errors == []


def test_disallowed_third_repeat_closes_pending_and_stops(monkeypatch):
    context, _, errors = _context(native=True)
    monkeypatch.setattr(
        tool_rejections.tool_persistence,
        "save_tool_bubble",
        lambda request: None,
    )

    outcome = tool_rejections.handle_disallowed_tool(
        context,
        "workspace.write",
        {},
        "call-1",
        {"workspace.read"},
        "disallowed|workspace.write|{}",
        2,
    )

    assert outcome.action is TurnCallAction.STOP_RUN
    assert context.conversation[-1]["tool_call_id"] == "call-2"
    assert errors == ["Repeated disallowed MCP tool call: workspace.write"]


def test_unknown_tool_error_is_not_reported_as_permission_denial(monkeypatch):
    context, _, _ = _context(native=True)
    monkeypatch.setattr(tool_rejections.tool_persistence, "save_tool_bubble", lambda request: None)

    tool_rejections.handle_disallowed_tool(
        context,
        "aifree__browser__observe_typo",
        {},
        "call-1",
        {"aifree.browser+observe"},
        "",
        0,
        tool_rejections.ToolResolutionInfo(
            raw_tool="aifree__browser__observe_typo",
            known_tools=frozenset({"aifree.browser+observe"}),
        ),
    )

    payload = context.conversation[0]["content"]
    assert "Unknown MCP tool name" in payload
    assert "not a permission denial" in payload
