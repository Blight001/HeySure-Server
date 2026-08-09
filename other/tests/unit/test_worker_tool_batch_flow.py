from dataclasses import replace
from types import SimpleNamespace

from ai_runtime.inference import (
    plan_transitions,
    tool_batch_flow,
    worker_tool_batch_flow,
)
from ai_runtime.inference.run_request import WorkerRequest
from ai_runtime.inference.tool_resolution import TurnCallAction


def _context():
    events = []
    request = WorkerRequest.create(
        run_id="run-a",
        user_id=7,
        ai_config_id=3,
        ai_kind="assistant",
        session_id="session-a",
        session_name="session name",
        model_user_content="request",
        current_user_message_id=11,
    )
    context = worker_tool_batch_flow.WorkerToolBatchContext(
        session=SimpleNamespace(),
        request=request,
        config=SimpleNamespace(mcp_enabled=True),
        model="model-a",
        system_prompt="prompt",
        conversation=[],
        saved_message=SimpleNamespace(id=9),
        effective_tools=frozenset({"workspace.read"}),
        native_tool_name_map={"workspace_read": "workspace.read"},
        native_tool_calls=True,
        turn_conversation_start=2,
        image_input_disabled=False,
        screenshot_messages=[{"role": "user", "content": "image"}],
        should_stop=lambda: False,
        stop_run=lambda: events.append("stopped"),
        complete_run=lambda: events.append("completed"),
        set_live_phase=lambda phase, tool="": events.append((phase, tool)),
        set_run_error=lambda error: events.append(("error", error)),
        auto_finalize_plan=lambda timestamp: events.append(("finalize", timestamp)),
    )
    return context, events


def _state():
    return worker_tool_batch_flow.WorkerToolBatchState(
        session_name="session name",
        exposed_tools=frozenset({"workspace.read"}),
        rejected_tool_signature="old",
        rejected_repeat=1,
        plan=plan_transitions.PlanFlowSnapshot(
            plan_state=SimpleNamespace(goal="ship"),
            awaiting_finish=False,
            phase_start_convo_index=3,
            phase_started_at=4.0,
            phase_mcp_statuses=[],
        ),
        last_batch_signature="previous",
        consecutive_same_batch=1,
    )


def _data():
    return worker_tool_batch_flow.WorkerToolBatchData(
        step_label="2/8",
        turn_calls=[{"tool": "workspace.read", "arguments": {"path": "a"}}],
    )


def _progress(monkeypatch, action):
    monkeypatch.setattr(
        worker_tool_batch_flow.tool_batch_flow,
        "evaluate_progress",
        lambda *args: tool_batch_flow.ProgressOutcome(
            action,
            tool_batch_flow.ProgressState("next-signature", 2),
        ),
    )
    monkeypatch.setattr(worker_tool_batch_flow, "ai_debug_stage", lambda *args: None)


def test_no_progress_requests_next_turn_and_updates_state(monkeypatch):
    context, events = _context()
    _progress(monkeypatch, tool_batch_flow.ProgressAction.NEXT_TURN)

    outcome = worker_tool_batch_flow.handle_tool_batch(context, _state(), _data())

    assert outcome.action is worker_tool_batch_flow.WorkerToolBatchAction.NEXT_TURN
    assert outcome.state.last_batch_signature == "next-signature"
    assert outcome.state.consecutive_same_batch == 2
    assert events == []


def test_no_progress_stop_completes_run(monkeypatch):
    context, events = _context()
    _progress(monkeypatch, tool_batch_flow.ProgressAction.STOP_RUN)

    outcome = worker_tool_batch_flow.handle_tool_batch(context, _state(), _data())

    assert outcome.action is worker_tool_batch_flow.WorkerToolBatchAction.STOP_RUN
    assert events == ["completed"]


def test_drained_batch_flushes_screenshots_and_returns_machine_state(monkeypatch):
    context, events = _context()
    _progress(monkeypatch, tool_batch_flow.ProgressAction.EXECUTE_BATCH)
    flushed = []

    class FakeMachine:
        def __init__(self, call_context, state):
            assert call_context.run_id == "run-a"
            assert call_context.user_id == 7
            self.state = replace(
                state,
                session_name="renamed",
                exposed_tools=frozenset({"workspace.read", "todo.manage"}),
                rejected_tool_signature="",
                rejected_repeat=0,
            )

        def execute(self, call, pending):
            return TurnCallAction.NEXT_CALL

    monkeypatch.setattr(worker_tool_batch_flow.turn_call_flow, "TurnCallMachine", FakeMachine)
    monkeypatch.setattr(
        worker_tool_batch_flow.tool_batch_flow,
        "execute_turn_batch",
        lambda conversation, calls, native, execute, duplicate: (
            duplicate(calls[0]) or TurnCallAction.NEXT_CALL
        ),
    )
    monkeypatch.setattr(
        worker_tool_batch_flow,
        "flush_screenshot_messages",
        lambda conversation, screenshots: flushed.append((conversation, screenshots)),
    )

    outcome = worker_tool_batch_flow.handle_tool_batch(context, _state(), _data())

    assert outcome.action is worker_tool_batch_flow.WorkerToolBatchAction.NEXT_TURN
    assert outcome.state.session_name == "renamed"
    assert "todo.manage" in outcome.state.exposed_tools
    assert flushed == [(context.conversation, context.screenshot_messages)]
    assert events[-1] == ("generating", "")


def test_batch_barrier_skips_screenshot_flush(monkeypatch):
    context, events = _context()
    _progress(monkeypatch, tool_batch_flow.ProgressAction.EXECUTE_BATCH)

    class FakeMachine:
        def __init__(self, call_context, state):
            self.state = state

        def execute(self, call, pending):
            return TurnCallAction.NEXT_TURN

    monkeypatch.setattr(worker_tool_batch_flow.turn_call_flow, "TurnCallMachine", FakeMachine)
    monkeypatch.setattr(
        worker_tool_batch_flow.tool_batch_flow,
        "execute_turn_batch",
        lambda *args: TurnCallAction.NEXT_TURN,
    )
    monkeypatch.setattr(
        worker_tool_batch_flow,
        "flush_screenshot_messages",
        lambda *args: events.append("flushed"),
    )

    outcome = worker_tool_batch_flow.handle_tool_batch(context, _state(), _data())

    assert outcome.action is worker_tool_batch_flow.WorkerToolBatchAction.NEXT_TURN
    assert "flushed" not in events


def test_batch_stop_is_terminal(monkeypatch):
    context, _ = _context()
    _progress(monkeypatch, tool_batch_flow.ProgressAction.EXECUTE_BATCH)

    class FakeMachine:
        def __init__(self, call_context, state):
            self.state = state

        def execute(self, call, pending):
            return TurnCallAction.STOP_RUN

    monkeypatch.setattr(worker_tool_batch_flow.turn_call_flow, "TurnCallMachine", FakeMachine)
    monkeypatch.setattr(
        worker_tool_batch_flow.tool_batch_flow,
        "execute_turn_batch",
        lambda *args: TurnCallAction.STOP_RUN,
    )

    outcome = worker_tool_batch_flow.handle_tool_batch(context, _state(), _data())

    assert outcome.action is worker_tool_batch_flow.WorkerToolBatchAction.STOP_RUN
