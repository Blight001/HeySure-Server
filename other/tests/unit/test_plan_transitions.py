from types import SimpleNamespace

from ai_runtime.inference import plan_transitions
from ai_runtime.inference.tool_resolution import TurnCallAction


def _context(*, native=True, current_message=None):
    phases = []
    completed = []
    finalized = []
    session = SimpleNamespace(
        get=lambda model, message_id: current_message,
        commit=lambda: None,
        rollback=lambda: None,
    )
    context = plan_transitions.PlanTransitionContext(
        session=session,
        conversation=[{"role": "assistant", "content": "working"}],
        pending=[{"id": "call-2", "tool": "workspace.read"}],
        screenshot_messages=[{"role": "user", "content": "image"}],
        user_id=9,
        ai_config_id=3,
        ai_kind="assistant",
        session_id="session-a",
        session_name="任务",
        model="model-a",
        native_tool_calls=native,
        system_prompt="system",
        current_user_message_id=17 if current_message else None,
        model_user_content="current request",
        set_live_phase=phases.append,
        complete_run=lambda: completed.append(True),
        auto_finalize_plan=finalized.append,
    )
    return context, phases, completed, finalized


def _snapshot(plan=None):
    return plan_transitions.PlanFlowSnapshot(
        plan_state=plan,
        awaiting_finish=False,
        phase_start_convo_index=1,
        phase_started_at=100.0,
        phase_mcp_statuses=[("workspace.read", False)],
    )


def test_delete_transition_closes_pending_calls_and_resets_plan():
    context, phases, _, _ = _context()

    transition = plan_transitions.handle_plan_transition(
        context,
        _snapshot(SimpleNamespace()),
        plan_transitions.ControlToolCall(
            tool="todo.manage",
            arguments={"action": "delete"},
            tool_result={"result": {"success": True}},
            failed=False,
            call_id="call-1",
        ),
    )

    assert transition.action is TurnCallAction.NEXT_TURN
    assert transition.snapshot.plan_state is None
    assert transition.snapshot.phase_mcp_statuses == []
    tool_responses = [
        item for item in context.conversation if item.get("role") == "tool"
    ]
    assert [item["tool_call_id"] for item in tool_responses] == [
        "call-1",
        "call-2",
    ]
    assert context.screenshot_messages == []
    assert phases == ["generating"]


def test_clear_transition_rebuilds_context_and_reanchors_active_phase():
    current_message = SimpleNamespace(
        user_id=9,
        ai_config_id=3,
        ai_kind="assistant",
        session_id="session-a",
        role="user",
        content="persisted request",
    )
    context, phases, _, _ = _context(current_message=current_message)

    transition = plan_transitions.handle_plan_transition(
        context,
        _snapshot(SimpleNamespace()),
        plan_transitions.ControlToolCall(
            tool="conversation.manage",
            arguments={"action": "clear"},
            tool_result={
                "result": {
                    "success": True,
                    "action": "clear",
                    "session_id": "session-a",
                }
            },
            failed=False,
            call_id="call-1",
        ),
    )

    assert transition.action is TurnCallAction.NEXT_TURN
    assert context.conversation[0] == {"role": "system", "content": "system"}
    assert context.conversation[1]["content"] == "current request"
    assert "旧上下文已从本轮模型上下文中移除" in context.conversation[2]["content"]
    assert transition.snapshot.phase_start_convo_index == len(context.conversation)
    assert phases == ["generating"]


def test_create_transition_reloads_plan_and_hands_over_phase(monkeypatch):
    context, phases, _, _ = _context()
    plan = SimpleNamespace(current_phase_seq=0)
    monkeypatch.setattr(plan_transitions, "_reload_plan", lambda _context: (plan, False))
    monkeypatch.setattr(
        plan_transitions,
        "_append_current_phase",
        lambda ctx, current: ctx.conversation.append({"role": "user", "content": "phase-1"}),
    )

    transition = plan_transitions.handle_plan_transition(
        context,
        _snapshot(),
        plan_transitions.ControlToolCall(
            tool="todo.manage",
            arguments={"goal": "ship", "phases": [{"goal": "build"}]},
            tool_result={"result": {"success": True}},
            failed=False,
            call_id="call-1",
        ),
    )

    assert transition.snapshot.plan_state is plan
    assert context.conversation[-1]["content"] == "phase-1"
    assert phases == ["generating"]


def test_edit_transition_auto_finalizes_when_last_phase_finishes(monkeypatch):
    context, phases, completed, finalized = _context()
    plan = SimpleNamespace(current_phase_seq=0)
    monkeypatch.setattr(plan_transitions, "_persist_phase_compaction", lambda *args: None)
    monkeypatch.setattr(plan_transitions, "_reload_plan", lambda _context: (plan, True))

    transition = plan_transitions.handle_plan_transition(
        context,
        _snapshot(plan),
        plan_transitions.ControlToolCall(
            tool="todo.manage",
            arguments={"action": "edit", "status": "completed"},
            tool_result={"result": {"success": True, "finished_phase": {"title": "build"}}},
            failed=False,
            call_id="call-1",
        ),
    )

    assert transition.action is TurnCallAction.STOP_RUN
    assert transition.snapshot.awaiting_finish is True
    assert len(finalized) == 1
    assert completed == [True]
    assert phases == ["idle"]
