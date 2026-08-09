from types import SimpleNamespace

from ai_runtime.inference import final_response_flow


class FakeSession:
    def __init__(self):
        self.added = []
        self.commits = 0

    def add(self, value):
        self.added.append(value)

    def commit(self):
        self.commits += 1


def _context(conversation=None):
    phases = []
    finalized = []
    notified = []
    context = final_response_flow.FinalResponseContext(
        session=FakeSession(),
        conversation=conversation if conversation is not None else [],
        saved_message=SimpleNamespace(tags=""),
        user_id=7,
        ai_config_id=3,
        ai_kind="assistant",
        session_id="session-a",
        session_name="任务",
        model="model-a",
        config=SimpleNamespace(),
        warning_template="warning",
        assistant_text="done",
        native_tool_calls=False,
        phase_started_at=12.5,
        set_live_phase=phases.append,
        auto_finalize_plan=finalized.append,
        notify_task_completion=lambda **kwargs: notified.append(kwargs),
    )
    return context, phases, finalized, notified


def _state(**overrides):
    values = {
        "markup_fallback_available": True,
        "pending_ai_reply_message_id": "",
        "plan_state": None,
        "awaiting_finish": False,
        "task_job": None,
    }
    values.update(overrides)
    return final_response_flow.FinalResponseState(**values)


def test_format_warning_is_persisted_and_starts_next_turn(monkeypatch):
    saved = []
    context, _, _, _ = _context()
    monkeypatch.setattr(final_response_flow, "_format_warning", lambda *args: "fix format")
    monkeypatch.setattr(
        final_response_flow,
        "_save_message",
        lambda session, user_id, message: saved.append(message),
    )

    outcome = final_response_flow.handle_final_response(context, _state())

    assert outcome.action is final_response_flow.FinalResponseAction.NEXT_TURN
    assert outcome.state.markup_fallback_available is False
    assert saved[0].tags == "system_notice_mcp_format_invalid"
    assert context.conversation[-1] == {"role": "user", "content": "fix format"}


def test_pending_user_inject_is_appended_before_completion(monkeypatch):
    context, phases, _, _ = _context()
    monkeypatch.setattr(final_response_flow, "_format_warning", lambda *args: "")
    monkeypatch.setattr(
        final_response_flow.chat_inject,
        "pop_pending_injects",
        lambda *args: ["new request"],
    )

    outcome = final_response_flow.handle_final_response(context, _state())

    assert outcome.action is final_response_flow.FinalResponseAction.NEXT_TURN
    assert context.conversation == [{"role": "user", "content": "new request"}]
    assert phases == ["generating"]


def test_awaiting_plan_is_auto_finalized_and_completed(monkeypatch):
    plan = SimpleNamespace(plan_id="plan-a")
    context, phases, finalized, _ = _context()
    monkeypatch.setattr(final_response_flow, "_format_warning", lambda *args: "")
    monkeypatch.setattr(final_response_flow, "_drain_pending_injects", lambda *args: [])
    monkeypatch.setattr(
        final_response_flow,
        "_reload_plan",
        lambda context, state: final_response_flow.replace(
            state, plan_state=plan, awaiting_finish=True
        ),
    )

    outcome = final_response_flow.handle_final_response(context, _state())

    assert outcome.action is final_response_flow.FinalResponseAction.COMPLETE_RUN
    assert outcome.state.plan_state is None
    assert finalized == [12.5]
    assert phases == ["idle"]


def test_simple_task_is_completed_when_no_plan_exists(monkeypatch):
    task = SimpleNamespace(job_id="job-a", status="running")
    context, _, _, notified = _context()
    monkeypatch.setattr(final_response_flow, "_format_warning", lambda *args: "")
    monkeypatch.setattr(final_response_flow, "_drain_pending_injects", lambda *args: [])
    monkeypatch.setattr(final_response_flow, "_active_plan_for_task", lambda *args: None)
    monkeypatch.setattr(final_response_flow, "_renew_simple_loop", lambda *args: None)

    outcome = final_response_flow.handle_final_response(
        context,
        _state(task_job=task),
    )

    assert outcome.action is final_response_flow.FinalResponseAction.COMPLETE_RUN
    assert task.status == "completed"
    assert context.session.added == [task]
    assert context.session.commits == 1
    assert notified[0]["job_id"] == "job-a"
