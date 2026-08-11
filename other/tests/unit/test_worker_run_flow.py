from dataclasses import replace
from types import SimpleNamespace

import pytest

from ai_runtime.inference import (
    worker_post_turn_flow,
    worker_run_flow,
    worker_setup,
    worker_tool_batch_flow,
    worker_turn_flow,
)
from ai_runtime.inference.run_request import WorkerRequest


def _request():
    return WorkerRequest.create(
        run_id="run-a",
        user_id=7,
        ai_config_id=3,
        ai_kind="assistant",
        session_id="session-a",
        session_name="session name",
        model_user_content="request",
        current_user_message_id=11,
    )


def _setup(*, task_runtime=False, max_steps=3):
    return worker_setup.WorkerSetup(
        user=SimpleNamespace(id=7),
        max_steps=max_steps,
        config=SimpleNamespace(mcp_enabled=True),
        api_key="secret",
        base_url="https://model.example/v1",
        model="model-a",
        system_prompt="prompt",
        warning_template="warning",
        auto_control={"compression_prompt": "compress"},
        task_job=SimpleNamespace(status="running"),
        is_task_runtime=task_runtime,
        effective_tool_allowlist=frozenset({"workspace.read"}),
        history=[],
        conversation=[],
    )


def _capabilities():
    return worker_setup.WorkerCapabilities(
        headers={"x-session": "a"},
        mcp_active=True,
        exposed_tool_allowlist=frozenset({"workspace.read"}),
        provider="openai",
        tool_protocol="native",
    )


def _machine(*, task_runtime=False, max_steps=3):
    setup = _setup(task_runtime=task_runtime, max_steps=max_steps)
    state = worker_run_flow.WorkerRunState(
        conversation=setup.conversation,
        session_name="session name",
        task_job=setup.task_job,
        exposed_tools={"workspace.read"},
        phase_started_at=2.0,
    )
    return worker_run_flow.WorkerRunMachine(
        SimpleNamespace(),
        _request(),
        setup,
        _capabilities(),
        state,
    )


def _patch_create_dependencies(monkeypatch, *, setup, capabilities, plan=None, awaiting=False):
    events = []
    monkeypatch.setattr(worker_run_flow.worker_setup, "prepare_worker", lambda *args: setup)
    monkeypatch.setattr(
        worker_run_flow.worker_setup,
        "prepare_capabilities",
        lambda *args: capabilities,
    )
    monkeypatch.setattr(worker_run_flow.time, "time", lambda: 10.0)
    monkeypatch.setattr(worker_run_flow, "ai_debug_stage", lambda *args: events.append("debug"))
    monkeypatch.setattr(
        worker_run_flow.run_context,
        "set_run_session_context",
        lambda value: events.append(("context", value)),
    )
    monkeypatch.setattr(
        worker_run_flow.plan_service,
        "get_active_plan",
        lambda *args: plan,
    )
    monkeypatch.setattr(
        worker_run_flow.plan_service,
        "awaiting_finish",
        lambda *args: awaiting,
    )
    return events


def test_create_initializes_task_notice_and_runtime_context(monkeypatch):
    setup = _setup(task_runtime=True)
    events = _patch_create_dependencies(
        monkeypatch,
        setup=setup,
        capabilities=_capabilities(),
    )
    monkeypatch.setattr(
        worker_run_flow.phase_context,
        "render_plan_required_notice",
        lambda: "plan required",
    )

    machine = worker_run_flow.WorkerRunMachine.create(SimpleNamespace(), _request())

    assert machine.state.phase_started_at == 10.0
    assert machine.state.conversation == [{"role": "user", "content": "plan required"}]
    assert events[0] == "debug"
    runtime_context = next(
        item[1]
        for item in events
        if isinstance(item, tuple) and item[0] == "context"
    )
    assert runtime_context["current_user_message_id"] == 11
    assert runtime_context["run_id"] == "run-a"


def test_create_recovers_and_reanchors_active_plan(monkeypatch):
    plan = SimpleNamespace(goal="ship")
    setup = _setup()
    _patch_create_dependencies(
        monkeypatch,
        setup=setup,
        capabilities=_capabilities(),
        plan=plan,
    )
    directives = []
    monkeypatch.setattr(
        worker_run_flow,
        "append_plan_directive",
        lambda conversation, session, current, awaiting_finish: directives.append(
            (conversation, current, awaiting_finish)
        ),
    )

    machine = worker_run_flow.WorkerRunMachine.create(SimpleNamespace(), _request())

    assert machine.state.plan_state is plan
    assert directives == [(machine.state.conversation, plan, False)]


def test_create_auto_finalizes_completed_plan(monkeypatch):
    plan = SimpleNamespace(goal="ship")
    setup = _setup(task_runtime=True)
    _patch_create_dependencies(
        monkeypatch,
        setup=setup,
        capabilities=_capabilities(),
        plan=plan,
        awaiting=True,
    )
    finalized = []
    monkeypatch.setattr(
        worker_run_flow,
        "finalize_plan",
        lambda context, current, final_phase_since_ts: finalized.append(
            (current, final_phase_since_ts)
        ),
    )

    machine = worker_run_flow.WorkerRunMachine.create(SimpleNamespace(), _request())

    assert finalized == [(plan, 10.0)]
    assert machine.state.plan_state is None
    assert machine.state.awaiting_finish is False
    assert machine.state.conversation == []


def test_plan_load_failure_falls_back_without_aborting(monkeypatch):
    machine = _machine()
    monkeypatch.setattr(
        worker_run_flow.plan_service,
        "get_active_plan",
        lambda *args: (_ for _ in ()).throw(RuntimeError("db unavailable")),
    )
    monkeypatch.setattr(worker_run_flow.logger, "exception", lambda *args: None)

    machine._load_plan()

    assert machine.state.plan_state is None


def test_run_honors_stop_before_starting_a_turn(monkeypatch):
    machine = _machine()
    events = []
    monkeypatch.setattr(worker_run_flow, "can_start_inference_step", lambda *args: True)
    monkeypatch.setattr(machine, "should_stop", lambda: True)
    monkeypatch.setattr(machine, "stop_run", lambda: events.append("stopped"))
    monkeypatch.setattr(machine, "_run_step", lambda: events.append("step"))

    machine.run()

    assert events == ["stopped"]


def test_run_emits_limit_notice_after_last_turn(monkeypatch):
    machine = _machine(max_steps=1)
    events = []
    monkeypatch.setattr(machine, "should_stop", lambda: False)
    monkeypatch.setattr(
        machine,
        "_run_step",
        lambda: worker_run_flow.WorkerRunStepAction.NEXT_TURN,
    )
    monkeypatch.setattr(machine, "_save_step_limit_notice", lambda: events.append("notice"))
    monkeypatch.setattr(machine, "complete_run", lambda: events.append("completed"))

    machine.run()

    assert machine.state.completed_steps == 1
    assert events == ["notice", "completed"]


def test_terminal_step_stops_loop_without_limit_notice(monkeypatch):
    machine = _machine()
    events = []
    monkeypatch.setattr(worker_run_flow, "can_start_inference_step", lambda *args: True)
    monkeypatch.setattr(machine, "should_stop", lambda: False)
    monkeypatch.setattr(
        machine,
        "_run_step",
        lambda: worker_run_flow.WorkerRunStepAction.STOP_RUN,
    )
    monkeypatch.setattr(machine, "_save_step_limit_notice", lambda: events.append("notice"))

    machine.run()

    assert events == []


def test_run_step_maps_stop_retry_and_missing_persistence(monkeypatch):
    machine = _machine()
    stop = SimpleNamespace(action=worker_turn_flow.WorkerTurnAction.STOP_RUN)
    retry = SimpleNamespace(action=worker_turn_flow.WorkerTurnAction.RETRY)
    missing = SimpleNamespace(
        action=worker_turn_flow.WorkerTurnAction.PROCEED,
        persisted_turn=None,
    )

    monkeypatch.setattr(machine, "_run_model_turn", lambda: stop)
    assert machine._run_step() is worker_run_flow.WorkerRunStepAction.STOP_RUN
    monkeypatch.setattr(machine, "_run_model_turn", lambda: retry)
    assert machine._run_step() is worker_run_flow.WorkerRunStepAction.NEXT_TURN
    monkeypatch.setattr(machine, "_run_model_turn", lambda: missing)
    with pytest.raises(RuntimeError, match="without persistence"):
        machine._run_step()


def test_run_step_maps_post_turn_and_tool_batch_actions(monkeypatch):
    machine = _machine()
    turn = SimpleNamespace(
        action=worker_turn_flow.WorkerTurnAction.PROCEED,
        persisted_turn=SimpleNamespace(),
    )
    monkeypatch.setattr(machine, "_run_model_turn", lambda: turn)
    monkeypatch.setattr(
        machine,
        "_run_post_turn",
        lambda current: SimpleNamespace(
            action=worker_post_turn_flow.PostTurnAction.NEXT_TURN
        ),
    )
    assert machine._run_step() is worker_run_flow.WorkerRunStepAction.NEXT_TURN

    events = []
    monkeypatch.setattr(
        machine,
        "_run_post_turn",
        lambda current: SimpleNamespace(
            action=worker_post_turn_flow.PostTurnAction.COMPLETE_RUN
        ),
    )
    monkeypatch.setattr(machine, "complete_run", lambda: events.append("completed"))
    assert machine._run_step() is worker_run_flow.WorkerRunStepAction.STOP_RUN
    assert events == ["completed"]

    monkeypatch.setattr(
        machine,
        "_run_post_turn",
        lambda current: SimpleNamespace(
            action=worker_post_turn_flow.PostTurnAction.EXECUTE_TOOLS
        ),
    )
    monkeypatch.setattr(
        machine,
        "_run_tool_batch",
        lambda current: worker_run_flow.WorkerRunStepAction.NEXT_TURN,
    )
    assert machine._run_step() is worker_run_flow.WorkerRunStepAction.NEXT_TURN


def test_model_turn_maps_context_policy_and_updates_state(monkeypatch):
    machine = _machine(task_runtime=True)
    machine.state.completed_steps = 2
    captured = {}
    next_state = worker_turn_flow.WorkerTurnState("reply-b", 2, True)

    def run_turn(context, request):
        captured["context"] = context
        captured["request"] = request
        return worker_turn_flow.WorkerTurnOutcome(
            worker_turn_flow.WorkerTurnAction.RETRY,
            next_state,
        )

    monkeypatch.setattr(worker_run_flow.worker_turn_flow, "run_worker_turn", run_turn)

    outcome = machine._run_model_turn()

    assert outcome.state is next_state
    assert captured["context"].run_id == "run-a"
    assert captured["request"].step_label == "2/3"
    assert captured["request"].policy.task_runtime is True
    assert machine.state.pending_reply_message_id == "reply-b"
    assert machine.state.image_input_disabled is True


def test_post_turn_and_tool_batch_states_round_trip(monkeypatch):
    machine = _machine()
    persisted = SimpleNamespace(
        saved_message=SimpleNamespace(id=9),
        tool_calls=[{"tool": "workspace.read"}],
        conversation_start=1,
    )
    turn = SimpleNamespace(
        persisted_turn=persisted,
        assistant_text="answer",
        native_tool_calls=True,
        native_tool_name_map={},
    )
    post_state = replace(
        machine._post_turn_state(),
        session_name="renamed",
        pending_reply_message_id="reply-c",
        compression_failed=True,
    )
    monkeypatch.setattr(
        worker_run_flow.worker_post_turn_flow,
        "handle_post_turn",
        lambda *args: worker_post_turn_flow.PostTurnOutcome(
            worker_post_turn_flow.PostTurnAction.EXECUTE_TOOLS,
            post_state,
        ),
    )

    machine._run_post_turn(turn)

    assert machine.state.session_name == "renamed"
    assert machine.state.pending_reply_message_id == "reply-c"
    assert machine.state.compression_failed is True

    batch_state = replace(
        machine._tool_batch_state(),
        exposed_tools=frozenset({"workspace.read", "todo.manage"}),
        last_batch_signature="batch-b",
        consecutive_same_batch=2,
    )
    monkeypatch.setattr(
        worker_run_flow.worker_tool_batch_flow,
        "handle_tool_batch",
        lambda *args: worker_tool_batch_flow.WorkerToolBatchOutcome(
            worker_tool_batch_flow.WorkerToolBatchAction.NEXT_TURN,
            batch_state,
        ),
    )

    action = machine._run_tool_batch(turn)

    assert action is worker_run_flow.WorkerRunStepAction.NEXT_TURN
    assert "todo.manage" in machine.state.exposed_tools
    assert machine.state.last_batch_signature == "batch-b"

    monkeypatch.setattr(
        worker_run_flow.worker_tool_batch_flow,
        "handle_tool_batch",
        lambda *args: worker_tool_batch_flow.WorkerToolBatchOutcome(
            worker_tool_batch_flow.WorkerToolBatchAction.STOP_RUN,
            batch_state,
        ),
    )
    assert machine._run_tool_batch(turn) is worker_run_flow.WorkerRunStepAction.STOP_RUN


def test_optional_plan_and_finalize_noop_paths(monkeypatch):
    machine = _machine()
    machine.request = replace(machine.request, ai_config_id=None)
    monkeypatch.setattr(
        worker_run_flow.plan_service,
        "get_active_plan",
        lambda *args: pytest.fail("plan lookup should be skipped"),
    )

    machine._load_plan()
    machine.auto_finalize_plan(2.0)

    assert machine.state.plan_state is None


def test_runtime_callbacks_and_step_notice_delegate(monkeypatch):
    machine = _machine(max_steps=5)
    events = []
    monkeypatch.setattr(worker_run_flow, "_run_should_stop", lambda run_id: True)
    monkeypatch.setattr(
        worker_run_flow,
        "_run_set_status",
        lambda *args, **kwargs: events.append(("status", args, kwargs)),
    )
    monkeypatch.setattr(
        worker_run_flow,
        "_set_run_live_phase",
        lambda *args: events.append(("phase", args)),
    )
    monkeypatch.setattr(
        worker_run_flow,
        "_set_run_live_text",
        lambda *args: events.append(("text", args)),
    )
    monkeypatch.setattr(
        worker_run_flow,
        "_set_run_live_usage",
        lambda *args: events.append(("usage", args)),
    )
    monkeypatch.setattr(
        worker_run_flow,
        "_save_message",
        lambda session, user_id, message: events.append(("message", message)),
    )

    assert machine.should_stop() is True
    machine.stop_run()
    machine.complete_run()
    machine.set_run_error("boom")
    machine.set_live_phase("calling_mcp", "workspace.read")
    machine.clear_live_text()
    machine.reset_live_usage()
    machine._save_step_limit_notice()

    assert sum(event[0] == "status" for event in events) == 3
    notice = next(event[1] for event in events if event[0] == "message")
    assert "5" in notice.content
