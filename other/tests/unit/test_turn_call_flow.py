from types import SimpleNamespace

from ai_runtime.inference import plan_transitions, turn_call_flow
from ai_runtime.inference.tool_execution import ToolExecutionResult
from ai_runtime.inference.tool_resolution import TurnCallAction
from ai_runtime.inference.tool_rejections import RejectionOutcome


class FakeSession:
    def __init__(self):
        self.active = False
        self.new = set()
        self.dirty = set()
        self.deleted = set()
        self.rollback_count = 0

    def in_transaction(self):
        return self.active

    def rollback(self):
        self.active = False
        self.rollback_count += 1

    def add(self, value):
        self.added = value

    def commit(self):
        self.committed = True


def _machine(*, stopped=False, mcp_enabled=True, allowed=None):
    events = []
    conversation = []
    saved = SimpleNamespace(id=9, tags="", session_name="before")
    context = turn_call_flow.TurnCallContext(
        session=FakeSession(),
        conversation=conversation,
        screenshot_messages=[],
        saved_message=saved,
        user_id=7,
        ai_config_id=3,
        ai_kind="assistant",
        session_id="session-a",
        model="model-a",
        run_id="run-a",
        config=SimpleNamespace(mcp_enabled=mcp_enabled),
        effective_tools=frozenset(allowed or {"workspace.read"}),
        native_tool_name_map={"workspace__read": "workspace.read"},
        native_tool_calls=True,
        system_prompt="prompt",
        current_user_message_id=11,
        model_user_content="question",
        turn_conversation_start=0,
        image_input_disabled=False,
        should_stop=lambda: stopped,
        stop_run=lambda: events.append("stopped"),
        complete_run=lambda: events.append("completed"),
        set_live_phase=lambda *args: events.append(args),
        set_run_error=lambda error: events.append(("error", error)),
        auto_finalize_plan=lambda timestamp: events.append(("finalize", timestamp)),
    )
    state = turn_call_flow.TurnCallState(
        session_name="before",
        exposed_tools=frozenset({"mcp.describe+tool"}),
        rejected_tool_signature="",
        rejected_repeat=0,
        plan=plan_transitions.PlanFlowSnapshot(
            plan_state=None,
            awaiting_finish=False,
            phase_start_convo_index=0,
            phase_started_at=1.0,
            phase_mcp_statuses=[],
        ),
    )
    return turn_call_flow.TurnCallMachine(context, state), events


def test_machine_stops_before_tool_execution():
    machine, events = _machine(stopped=True)

    action = machine.execute(
        {"tool": "workspace.read", "arguments": {}, "id": "call-1"},
        [],
    )

    assert action is TurnCallAction.STOP_RUN
    assert events == ["stopped"]


def test_machine_carries_rejection_state_between_calls(monkeypatch):
    machine, _ = _machine(mcp_enabled=False)
    monkeypatch.setattr(
        turn_call_flow.tool_rejections,
        "handle_mcp_disabled",
        lambda *args: RejectionOutcome(
            "disabled|workspace.read|{}",
            2,
            TurnCallAction.NEXT_TURN,
        ),
    )

    action = machine.execute(
        {"tool": "workspace.read", "arguments": {}, "id": "call-1"},
        [],
    )

    assert action is TurnCallAction.NEXT_TURN
    assert machine.state.rejected_repeat == 2
    assert machine.state.rejected_tool_signature.startswith("disabled|")


def test_machine_rejects_tool_outside_effective_allowlist(monkeypatch):
    machine, _ = _machine()
    monkeypatch.setattr(
        turn_call_flow.tool_rejections,
        "handle_disallowed_tool",
        lambda *args: RejectionOutcome(
            "disallowed|workspace.write|{}",
            1,
            TurnCallAction.NEXT_CALL,
        ),
    )

    action = machine.execute(
        {"tool": "workspace.write", "arguments": {}, "id": "call-1"},
        [],
    )

    assert action is TurnCallAction.NEXT_CALL
    assert machine.state.rejected_repeat == 1


def test_machine_maps_and_executes_joined_legacy_tools(monkeypatch):
    machine, _ = _machine(allowed={"workspace.read", "workspace.write"})
    monkeypatch.setattr(
        turn_call_flow,
        "split_concatenated_native_tool_name",
        lambda *args: ["workspace__read", "workspace.write"],
    )
    captured = {}
    monkeypatch.setattr(
        turn_call_flow.tool_persistence,
        "execute_and_persist_joined_batch",
        lambda request, context: captured.update(request=request, context=context)
        or SimpleNamespace(stopped=False, failed=False, items=({"result": "ok"},)),
    )
    monkeypatch.setattr(
        turn_call_flow,
        "append_joined_tool_response",
        lambda *args, **kwargs: captured.update(appended=True),
    )

    action = machine.execute(
        {"tool": "joined", "arguments": {"path": "a"}, "id": "call-1"},
        [],
    )

    assert action is TurnCallAction.NEXT_CALL
    assert captured["request"].tools == ("workspace.read", "workspace.write")
    assert captured["appended"] is True


def test_machine_stops_when_joined_batch_observes_cancellation(monkeypatch):
    machine, events = _machine()
    monkeypatch.setattr(
        turn_call_flow,
        "split_concatenated_native_tool_name",
        lambda *args: ["workspace__read"],
    )
    monkeypatch.setattr(
        turn_call_flow.tool_persistence,
        "execute_and_persist_joined_batch",
        lambda *args: SimpleNamespace(stopped=True, failed=False, items=()),
    )

    action = machine.execute(
        {"tool": "joined", "arguments": {}, "id": "call-1"},
        [],
    )

    assert action is TurnCallAction.STOP_RUN
    assert events == ["stopped"]


def test_machine_persists_regular_result_and_returns_explicit_state(monkeypatch):
    machine, events = _machine()
    execution = ToolExecutionResult(
        result={"result": {"success": True}},
        failed=False,
        error="",
        display_text="ok",
        latency=0.25,
    )
    monkeypatch.setattr(turn_call_flow, "execute_tool_call", lambda *args: execution)
    monkeypatch.setattr(
        turn_call_flow.tool_persistence,
        "record_tool_call",
        lambda record: events.append(("record", record.tool)),
    )

    def apply_metadata(context, *args):
        context.exposed_tools.add("workspace.read")
        return "renamed"

    monkeypatch.setattr(
        turn_call_flow.tool_metadata,
        "apply_tool_metadata",
        apply_metadata,
    )
    monkeypatch.setattr(
        turn_call_flow.tool_metadata,
        "apply_session_rename",
        lambda saved, current, renamed: renamed or current,
    )
    monkeypatch.setattr(
        turn_call_flow.tool_media,
        "screenshot_display_ref",
        lambda *args: {},
    )
    monkeypatch.setattr(
        turn_call_flow.tool_persistence,
        "save_tool_bubble",
        lambda request: events.append(("bubble", request.session_name)),
    )
    monkeypatch.setattr(
        turn_call_flow.plan_transitions,
        "handle_plan_transition",
        lambda *args: None,
    )
    monkeypatch.setattr(
        turn_call_flow,
        "append_ordinary_tool_response",
        lambda *args: events.append("response"),
    )

    action = machine.execute(
        {"tool": "workspace.read", "arguments": {"path": "a"}, "id": "call-1"},
        [],
    )

    assert action is TurnCallAction.NEXT_CALL
    assert machine.state.session_name == "renamed"
    assert "workspace.read" in machine.state.exposed_tools
    assert ("record", "workspace.read") in events
    assert ("bubble", "renamed") in events
    assert "response" in events


def test_machine_releases_read_transaction_before_regular_tool(monkeypatch):
    machine, _ = _machine()
    machine.context.session.active = True
    execution = ToolExecutionResult(
        result={"result": {"success": True}},
        failed=False,
        error="",
        display_text="ok",
        latency=0.1,
    )

    def execute(*args):
        assert machine.context.session.active is False
        return execution

    monkeypatch.setattr(turn_call_flow, "execute_tool_call", execute)
    monkeypatch.setattr(machine, "_record_execution", lambda *args: None)
    monkeypatch.setattr(machine, "_apply_metadata", lambda *args: None)
    monkeypatch.setattr(machine, "_persist_execution", lambda *args: None)
    monkeypatch.setattr(machine, "_plan_transition", lambda *args: None)
    monkeypatch.setattr(turn_call_flow, "append_ordinary_tool_response", lambda *args: None)

    action = machine.execute(
        {"tool": "workspace.read", "arguments": {}, "id": "call-1"},
        [],
    )

    assert action is TurnCallAction.NEXT_CALL
    assert machine.context.session.rollback_count == 1


def test_machine_releases_read_transaction_before_each_joined_tool(monkeypatch):
    machine, _ = _machine(allowed={"workspace.read", "workspace.write"})
    session = machine.context.session
    session.active = True
    monkeypatch.setattr(
        turn_call_flow,
        "split_concatenated_native_tool_name",
        lambda *args: ["workspace__read", "workspace.write"],
    )

    def execute_batch(request, context):
        context.mark_waiting("workspace.read")
        assert session.active is False
        session.active = True
        context.mark_waiting("workspace.write")
        assert session.active is False
        return SimpleNamespace(stopped=False, failed=False, items=())

    monkeypatch.setattr(
        turn_call_flow.tool_persistence,
        "execute_and_persist_joined_batch",
        execute_batch,
    )
    monkeypatch.setattr(turn_call_flow, "append_joined_tool_response", lambda *args, **kwargs: None)

    action = machine.execute(
        {"tool": "joined", "arguments": {}, "id": "call-1"},
        [],
    )

    assert action is TurnCallAction.NEXT_CALL
    assert session.rollback_count == 2


def test_machine_returns_control_transition_snapshot(monkeypatch):
    machine, _ = _machine()
    execution = ToolExecutionResult(
        result={"result": {"success": True}},
        failed=False,
        error="",
        display_text="ok",
        latency=0.1,
    )
    next_snapshot = plan_transitions.PlanFlowSnapshot(
        plan_state=SimpleNamespace(id=8),
        awaiting_finish=False,
        phase_start_convo_index=4,
        phase_started_at=2.0,
        phase_mcp_statuses=[],
    )
    monkeypatch.setattr(turn_call_flow, "execute_tool_call", lambda *args: execution)
    monkeypatch.setattr(machine, "_record_execution", lambda *args: None)
    monkeypatch.setattr(machine, "_apply_metadata", lambda *args: None)
    monkeypatch.setattr(machine, "_persist_execution", lambda *args: None)
    monkeypatch.setattr(
        machine,
        "_plan_transition",
        lambda *args: plan_transitions.PlanTransition(
            TurnCallAction.NEXT_TURN,
            next_snapshot,
        ),
    )

    action = machine.execute(
        {"tool": "workspace.read", "arguments": {}, "id": "call-1"},
        [],
    )

    assert action is TurnCallAction.NEXT_TURN
    assert machine.state.plan is next_snapshot
