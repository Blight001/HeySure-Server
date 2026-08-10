from types import SimpleNamespace

from ai_runtime.inference import step_preparation, turn_result, worker_turn_flow
from ai_runtime.inference.model_error_flow import ModelErrorDecision


class TrackingSession:
    def __init__(self, *, active=True):
        self.active = active
        self.new = set()
        self.dirty = set()
        self.deleted = set()
        self.rollback_count = 0

    def in_transaction(self):
        return self.active

    def rollback(self):
        self.active = False
        self.rollback_count += 1


def _context(conversation=None, session=None):
    events = []
    context = worker_turn_flow.WorkerTurnContext(
        session=session or SimpleNamespace(),
        conversation=conversation if conversation is not None else [],
        user_id=7,
        ai_config_id=3,
        ai_kind="assistant",
        session_id="session-a",
        model="model-a",
        system_prompt="prompt",
        run_id="run-a",
        provider="openai_compat",
        base_url="https://model.example/v1",
        api_key="secret",
        headers={"Authorization": "Bearer secret"},
        should_stop=lambda: False,
        stop_run=lambda: events.append("stopped"),
        set_live_phase=lambda phase: events.append(("phase", phase)),
        set_run_error=lambda error: events.append(("error", error)),
        clear_live_text=lambda: events.append("text-cleared"),
        reset_live_usage=lambda: events.append("usage-reset"),
    )
    return context, events


def _request(*, errors=1, image_disabled=False):
    return worker_turn_flow.WorkerTurnRequest(
        step_label="2/40",
        session_name="session name",
        state=worker_turn_flow.WorkerTurnState(
            pending_reply_message_id="old-reply",
            consecutive_errors=errors,
            image_input_disabled=image_disabled,
        ),
        policy=worker_turn_flow.WorkerTurnPolicy(
            mcp_active=True,
            exposed_tools=frozenset({"workspace.read"}),
            allowed_tools=frozenset({"workspace.read"}),
            task_runtime=False,
            plan_active=False,
            awaiting_finish=False,
            tool_protocol="auto",
        ),
    )


def _prepare_dependencies(monkeypatch, *, pending="new-reply"):
    monkeypatch.setattr(
        worker_turn_flow.step_preparation,
        "ingest_step_messages",
        lambda context, previous: pending,
    )
    monkeypatch.setattr(
        worker_turn_flow.model_error_flow,
        "repair_missing_tool_responses",
        lambda *args: [],
    )
    monkeypatch.setattr(
        worker_turn_flow.step_preparation,
        "select_tool_exposure",
        lambda request: step_preparation.ToolExposure(
            current_tools=frozenset({"workspace.read"}),
            provider_tools=[{"type": "function"}],
            native_name_map={"workspace__read": "workspace.read"},
        ),
    )


def test_successful_turn_resets_errors_and_returns_persisted_result(monkeypatch):
    context, events = _context()
    _prepare_dependencies(monkeypatch)
    stream_result = SimpleNamespace(
        stopped=False,
        assistant_text="working",
        has_native_tc=True,
        finish_reason="tool_calls",
    )
    captured = {}
    monkeypatch.setattr(
        worker_turn_flow.model_gateway,
        "run_model_turn",
        lambda request: captured.update(model_request=request) or stream_result,
    )
    persisted = turn_result.PersistedAssistantTurn(
        saved_message=SimpleNamespace(id=8),
        tool_calls=[{"tool": "workspace.read"}],
        conversation_start=3,
        token_triplet="10/2/12",
    )
    monkeypatch.setattr(
        worker_turn_flow.turn_result,
        "persist_assistant_turn",
        lambda context, result, latency: captured.update(
            persist_context=context,
            latency=latency,
        ) or persisted,
    )

    outcome = worker_turn_flow.run_worker_turn(context, _request())

    assert outcome.action is worker_turn_flow.WorkerTurnAction.PROCEED
    assert outcome.persisted_turn is persisted
    assert outcome.assistant_text == "working"
    assert outcome.native_tool_calls is True
    assert outcome.native_tool_name_map == {"workspace__read": "workspace.read"}
    assert outcome.state.pending_reply_message_id == "new-reply"
    assert outcome.state.consecutive_errors == 0
    assert captured["model_request"].provider_tools == [{"type": "function"}]
    assert captured["persist_context"].session_name == "session name"
    assert captured["latency"] >= 0
    assert events[-2:] == ["text-cleared", "usage-reset"]


def test_model_request_releases_autobegun_read_transaction(monkeypatch):
    session = TrackingSession()
    context, events = _context(session=session)
    _prepare_dependencies(monkeypatch)

    def run_model(request):
        assert session.active is False
        return SimpleNamespace(stopped=True)

    monkeypatch.setattr(worker_turn_flow.model_gateway, "run_model_turn", run_model)

    outcome = worker_turn_flow.run_worker_turn(context, _request())

    assert outcome.action is worker_turn_flow.WorkerTurnAction.STOP_RUN
    assert session.rollback_count == 1
    assert events == ["stopped"]


def test_model_error_returns_retry_with_explicit_next_state(monkeypatch):
    context, _ = _context()
    _prepare_dependencies(monkeypatch)
    monkeypatch.setattr(
        worker_turn_flow.model_gateway,
        "run_model_turn",
        lambda request: (_ for _ in ()).throw(RuntimeError("upstream down")),
    )
    monkeypatch.setattr(
        worker_turn_flow,
        "_extract_mcp_error",
        lambda exc: "normalized upstream error",
    )
    captured = {}
    monkeypatch.setattr(
        worker_turn_flow.model_error_flow,
        "handle_model_error",
        lambda context, error, count, image_disabled: captured.update(
            error=error,
            count=count,
        ) or ModelErrorDecision(2, True, False),
    )

    outcome = worker_turn_flow.run_worker_turn(context, _request())

    assert outcome.action is worker_turn_flow.WorkerTurnAction.RETRY
    assert outcome.state.consecutive_errors == 2
    assert outcome.state.image_input_disabled is True
    assert outcome.state.pending_reply_message_id == "new-reply"
    assert captured == {"error": "normalized upstream error", "count": 1}


def test_terminal_model_error_returns_stop_without_fake_persistence(monkeypatch):
    context, _ = _context()
    _prepare_dependencies(monkeypatch)
    monkeypatch.setattr(
        worker_turn_flow.model_gateway,
        "run_model_turn",
        lambda request: (_ for _ in ()).throw(RuntimeError("still down")),
    )
    monkeypatch.setattr(worker_turn_flow, "_extract_mcp_error", lambda exc: str(exc))
    monkeypatch.setattr(
        worker_turn_flow.model_error_flow,
        "handle_model_error",
        lambda *args: ModelErrorDecision(3, False, True),
    )

    outcome = worker_turn_flow.run_worker_turn(context, _request(errors=2))

    assert outcome.action is worker_turn_flow.WorkerTurnAction.STOP_RUN
    assert outcome.persisted_turn is None
    assert outcome.state.consecutive_errors == 3


def test_stream_cancellation_marks_run_stopped(monkeypatch):
    context, events = _context()
    _prepare_dependencies(monkeypatch)
    monkeypatch.setattr(
        worker_turn_flow.model_gateway,
        "run_model_turn",
        lambda request: SimpleNamespace(stopped=True),
    )

    outcome = worker_turn_flow.run_worker_turn(context, _request())

    assert outcome.action is worker_turn_flow.WorkerTurnAction.STOP_RUN
    assert events == ["stopped"]


def test_disabled_image_input_is_degraded_before_request(monkeypatch):
    conversation = [{"role": "user", "content": "image"}]
    context, _ = _context(conversation)
    _prepare_dependencies(monkeypatch)
    monkeypatch.setattr(
        worker_turn_flow.tool_media,
        "degrade_image_messages_to_text",
        lambda messages: 2,
    )
    monkeypatch.setattr(
        worker_turn_flow.tool_media,
        "image_input_degraded_feedback",
        lambda reason, count: f"degraded {count}",
    )
    observed = {}

    def stop_after_prepare(request):
        observed["last_message"] = context.conversation[-1]
        return SimpleNamespace(stopped=True)

    monkeypatch.setattr(
        worker_turn_flow.model_gateway,
        "run_model_turn",
        stop_after_prepare,
    )

    worker_turn_flow.run_worker_turn(
        context,
        _request(image_disabled=True),
    )

    assert observed["last_message"] == {
        "role": "user",
        "content": "degraded 2",
    }
