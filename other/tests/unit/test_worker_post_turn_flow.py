from types import SimpleNamespace

from ai_runtime.inference import (
    compression_flow,
    final_response_flow,
    worker_post_turn_flow,
)
from ai_runtime.inference.run_request import WorkerRequest


def _context(*, task_runtime=False):
    events = []
    request = WorkerRequest.create(
        run_id="run-a",
        user_id=7,
        ai_config_id=3,
        ai_kind="assistant",
        session_id="session-a",
        session_name="session name",
    )
    setup = SimpleNamespace(
        user=SimpleNamespace(),
        config=SimpleNamespace(),
        model="model-a",
        api_key="secret",
        base_url="https://model.example/v1",
        system_prompt="prompt",
        warning_template="warning",
        auto_control={"compression_prompt": "compress"},
        is_task_runtime=task_runtime,
    )
    context = worker_post_turn_flow.PostTurnContext(
        session=SimpleNamespace(),
        request=request,
        setup=setup,
        reset_live_usage=lambda: events.append("usage-reset"),
        set_live_phase=lambda phase: events.append(("phase", phase)),
        inject_flow_directive=lambda convo: events.append("directive"),
        auto_finalize_plan=lambda timestamp: events.append(("finalize", timestamp)),
    )
    return context, events


def _state(*, plan=None, awaiting=False, task_job=None):
    return worker_post_turn_flow.PostTurnState(
        conversation=[],
        session_name="session name",
        plan_state=plan,
        awaiting_finish=awaiting,
        phase_start_convo_index=1,
        phase_started_at=2.0,
        phase_mcp_statuses=[("workspace.read", "success")],
        compression_failed=False,
        task_job=task_job,
        markup_fallback_available=True,
        pending_reply_message_id="reply-a",
    )


def _turn(*, calls=None, native=True):
    return worker_post_turn_flow.PostTurnData(
        saved_message=SimpleNamespace(id=9),
        assistant_text="answer",
        native_tool_calls=native,
        turn_calls=list(calls or []),
    )


def _compression_passthrough(monkeypatch, *, auto_continue=False):
    monkeypatch.setattr(
        worker_post_turn_flow.compression_flow,
        "handle_manual_compression",
        lambda context, state, calls, native: compression_flow.CompressionDecision(
            False,
            False,
            state,
        ),
    )
    monkeypatch.setattr(
        worker_post_turn_flow.compression_flow,
        "maybe_auto_compress",
        lambda context, state, calls, finished: compression_flow.CompressionDecision(
            False,
            auto_continue,
            state,
        ),
    )


def test_manual_compression_returns_next_turn_with_reanchored_state(monkeypatch):
    context, _ = _context()
    rebuilt = compression_flow.CompressionState(
        conversation=[{"role": "system", "content": "summary"}],
        compression_failed=False,
        phase_start_convo_index=1,
        phase_started_at=5.0,
        phase_mcp_statuses=[],
    )
    monkeypatch.setattr(
        worker_post_turn_flow.compression_flow,
        "handle_manual_compression",
        lambda *args: compression_flow.CompressionDecision(True, True, rebuilt),
    )

    outcome = worker_post_turn_flow.handle_post_turn(
        context,
        _state(),
        _turn(calls=[{"tool": "conversation.manage", "arguments": {}}]),
    )

    assert outcome.action is worker_post_turn_flow.PostTurnAction.NEXT_TURN
    assert outcome.state.conversation == rebuilt.conversation
    assert outcome.state.phase_started_at == 5.0


def test_auto_compression_refreshes_task_job_and_can_continue(monkeypatch):
    context, _ = _context(task_runtime=True)
    latest_job = SimpleNamespace(status="running")
    _compression_passthrough(monkeypatch, auto_continue=True)
    monkeypatch.setattr(
        worker_post_turn_flow,
        "_load_task_job_by_session",
        lambda *args: latest_job,
    )

    outcome = worker_post_turn_flow.handle_post_turn(
        context,
        _state(task_job=SimpleNamespace(status="queued")),
        _turn(calls=[{"tool": "workspace.read"}]),
    )

    assert outcome.action is worker_post_turn_flow.PostTurnAction.NEXT_TURN
    assert outcome.state.task_job is latest_job


def test_allowed_tool_batch_proceeds_to_explicit_execution(monkeypatch):
    context, _ = _context(task_runtime=True)
    _compression_passthrough(monkeypatch)
    monkeypatch.setattr(worker_post_turn_flow, "_load_task_job_by_session", lambda *args: None)

    outcome = worker_post_turn_flow.handle_post_turn(
        context,
        _state(plan=SimpleNamespace(goal="ship")),
        _turn(calls=[{"tool": "workspace.read"}]),
    )

    assert outcome.action is worker_post_turn_flow.PostTurnAction.EXECUTE_TOOLS


def test_finish_gate_closes_native_calls_and_requests_next_turn(monkeypatch):
    context, events = _context(task_runtime=True)
    _compression_passthrough(monkeypatch)
    monkeypatch.setattr(worker_post_turn_flow, "_load_task_job_by_session", lambda *args: None)
    calls = [{"tool": "workspace.read", "id": "call-1"}]
    state = _state(plan=SimpleNamespace(goal="ship"), awaiting=True)

    outcome = worker_post_turn_flow.handle_post_turn(
        context,
        state,
        _turn(calls=calls),
    )

    assert outcome.action is worker_post_turn_flow.PostTurnAction.NEXT_TURN
    assert state.conversation[-1]["role"] == "tool"
    assert state.conversation[-1]["tool_call_id"] == "call-1"
    assert events[-1] == ("phase", "generating")


def test_finish_gate_allows_todo_manage(monkeypatch):
    context, _ = _context(task_runtime=True)
    _compression_passthrough(monkeypatch)
    monkeypatch.setattr(worker_post_turn_flow, "_load_task_job_by_session", lambda *args: None)

    outcome = worker_post_turn_flow.handle_post_turn(
        context,
        _state(plan=SimpleNamespace(goal="ship"), awaiting=True),
        _turn(calls=[{"tool": "todo.manage"}]),
    )

    assert outcome.action is worker_post_turn_flow.PostTurnAction.EXECUTE_TOOLS


def test_text_flow_violation_appends_user_notice(monkeypatch):
    context, _ = _context(task_runtime=True)
    _compression_passthrough(monkeypatch)
    monkeypatch.setattr(worker_post_turn_flow, "_load_task_job_by_session", lambda *args: None)
    state = _state(plan=SimpleNamespace(goal="ship"), awaiting=True)

    worker_post_turn_flow.handle_post_turn(
        context,
        state,
        _turn(calls=[{"tool": "workspace.read"}], native=False),
    )

    assert state.conversation[-1]["role"] == "user"


def test_final_response_maps_next_and_complete_actions(monkeypatch):
    context, _ = _context()
    _compression_passthrough(monkeypatch)
    source_state = _state()

    def final_outcome(action):
        return final_response_flow.FinalResponseOutcome(
            action,
            final_response_flow.FinalResponseState(
                markup_fallback_available=False,
                pending_ai_reply_message_id="",
                plan_state=None,
                awaiting_finish=False,
                task_job=None,
            ),
        )

    monkeypatch.setattr(
        worker_post_turn_flow.final_response_flow,
        "handle_final_response",
        lambda *args: final_outcome(final_response_flow.FinalResponseAction.NEXT_TURN),
    )
    retry = worker_post_turn_flow.handle_post_turn(context, source_state, _turn())
    monkeypatch.setattr(
        worker_post_turn_flow.final_response_flow,
        "handle_final_response",
        lambda *args: final_outcome(final_response_flow.FinalResponseAction.COMPLETE_RUN),
    )
    complete = worker_post_turn_flow.handle_post_turn(context, source_state, _turn())

    assert retry.action is worker_post_turn_flow.PostTurnAction.NEXT_TURN
    assert retry.state.markup_fallback_available is False
    assert complete.action is worker_post_turn_flow.PostTurnAction.COMPLETE_RUN
