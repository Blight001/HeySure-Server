from types import SimpleNamespace

from ai_runtime.inference import compression_flow


def _context(*, plan=None):
    usage_resets = []
    phases = []
    directives = []
    context = compression_flow.CompressionContext(
        session=SimpleNamespace(),
        user=SimpleNamespace(conversation_auto_compress_enabled=True),
        config=SimpleNamespace(ai_role="digital_member", token_limit=100),
        user_id=9,
        ai_config_id=3,
        ai_kind="assistant",
        session_id="session-a",
        session_name="任务",
        model="model-a",
        api_key="key",
        base_url="http://model",
        system_prompt="system",
        compression_prompt="compress",
        plan_state=plan,
        reset_live_usage=lambda: usage_resets.append(True),
        set_generating=lambda: phases.append("generating"),
        inject_flow_directive=lambda convo: directives.append(convo),
    )
    return context, usage_resets, phases, directives


def _state(conversation=None):
    return compression_flow.CompressionState(
        conversation=conversation or [{"role": "system", "content": "system"}],
        compression_failed=False,
        phase_start_convo_index=1,
        phase_started_at=100.0,
        phase_mcp_statuses=[("workspace.read", False)],
    )


def test_manual_compression_ignores_unrelated_turn():
    context, _, _, _ = _context()

    decision = compression_flow.handle_manual_compression(
        context,
        _state(),
        [{"tool": "workspace.read", "arguments": {}, "id": "call-1"}],
        True,
    )

    assert decision.handled is False
    assert decision.continue_loop is False


def test_manual_compression_rebuilds_and_reanchors_active_plan(monkeypatch):
    context, resets, phases, _ = _context(plan=SimpleNamespace())
    rebuilt = [{"role": "system", "content": "summary"}]
    bubbles = []

    def compress(*_args, **kwargs):
        kwargs["on_tool_result"](True, "完成")
        return rebuilt

    monkeypatch.setattr(compression_flow, "_compress", compress)
    monkeypatch.setattr(
        compression_flow.tool_persistence,
        "save_tool_bubble",
        lambda request: bubbles.append(request),
    )

    decision = compression_flow.handle_manual_compression(
        context,
        _state(),
        [{
            "tool": "conversation.manage",
            "arguments": {"action": "compress", "keep_recent": 30},
            "id": "call-1",
        }],
        True,
    )

    assert decision.handled is True
    assert decision.continue_loop is True
    assert decision.state.conversation[-1]["role"] == "user"
    assert decision.state.phase_start_convo_index == len(rebuilt)
    assert decision.state.phase_mcp_statuses == []
    assert resets == [True]
    assert phases == ["generating"]
    assert bubbles[0].tool == "conversation.manage"
    assert bubbles[0].arguments == {"action": "compress", "keep_recent": 30}


def test_manual_failure_answers_compress_and_pending_native_calls(monkeypatch):
    context, _, phases, _ = _context()
    state = _state()
    monkeypatch.setattr(compression_flow, "_compress", lambda *args, **kwargs: None)
    calls = [
        {
            "tool": "conversation.manage",
            "arguments": {"action": "compress"},
            "id": "call-1",
        },
        {"tool": "workspace.read", "arguments": {}, "id": "call-2"},
    ]

    decision = compression_flow.handle_manual_compression(
        context,
        state,
        calls,
        True,
    )

    assert [item["tool_call_id"] for item in state.conversation[-2:]] == [
        "call-1",
        "call-2",
    ]
    assert decision.state is state
    assert phases == ["generating"]
    assert '"success": false' in state.conversation[-2]["content"]


def test_auto_compression_rebuilds_and_reinjects_plan(monkeypatch):
    context, resets, _, directives = _context(plan=SimpleNamespace())
    rebuilt = [{"role": "system", "content": "summary"}]
    bubbles = []

    def compress(*_args, **kwargs):
        kwargs["on_tool_result"](True, "完成")
        return rebuilt

    monkeypatch.setattr(compression_flow, "_session_total_tokens", lambda *args: 150)
    monkeypatch.setattr(compression_flow, "_compress", compress)
    monkeypatch.setattr(
        compression_flow.tool_persistence,
        "save_tool_bubble",
        lambda request: bubbles.append(request),
    )

    decision = compression_flow.maybe_auto_compress(
        context,
        _state(),
        [{"tool": "workspace.read"}],
        False,
    )

    assert decision.handled is True
    assert decision.continue_loop is True
    assert decision.state.conversation is rebuilt
    assert resets == [True]
    assert directives == [rebuilt]
    assert bubbles[0].arguments == {"action": "compress", "trigger": "auto"}


def test_auto_compression_failure_disables_retry_for_run(monkeypatch):
    context, _, _, _ = _context()
    monkeypatch.setattr(compression_flow, "_session_total_tokens", lambda *args: 150)
    monkeypatch.setattr(compression_flow, "_compress", lambda *args, **kwargs: None)

    decision = compression_flow.maybe_auto_compress(
        context,
        _state(),
        [{"tool": "workspace.read"}],
        False,
    )

    assert decision.handled is True
    assert decision.continue_loop is False
    assert decision.state.compression_failed is True
