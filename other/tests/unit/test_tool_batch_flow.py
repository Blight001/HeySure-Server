from types import SimpleNamespace

from ai_runtime.inference import tool_batch_flow
from ai_runtime.inference.tool_resolution import TurnCallAction


def _call(call_id="call-1", query="x"):
    return {
        "id": call_id,
        "tool": "knowledge.search",
        "arguments": {"query": query},
    }


def _context(native=True):
    phases = []
    context = tool_batch_flow.ProgressContext(
        session=SimpleNamespace(),
        conversation=[],
        user_id=7,
        ai_config_id=3,
        ai_kind="assistant",
        session_id="session-a",
        session_name="任务",
        model="model-a",
        native_tool_calls=native,
        set_live_phase=phases.append,
    )
    return context, phases


def test_first_batch_is_allowed_and_state_is_recorded():
    context, phases = _context()

    outcome = tool_batch_flow.evaluate_progress(
        context,
        tool_batch_flow.ProgressState("", 0),
        [_call()],
    )

    assert outcome.action is tool_batch_flow.ProgressAction.EXECUTE_BATCH
    assert outcome.state.consecutive_same_batch == 1
    assert phases == []


def test_second_identical_batch_is_answered_without_execution():
    context, phases = _context()
    signature = tool_batch_flow.batch_signature([_call()])

    outcome = tool_batch_flow.evaluate_progress(
        context,
        tool_batch_flow.ProgressState(signature, 1),
        [_call()],
    )

    assert outcome.action is tool_batch_flow.ProgressAction.NEXT_TURN
    assert context.conversation[0]["role"] == "tool"
    assert context.conversation[0]["tool_call_id"] == "call-1"
    assert phases == ["generating"]


def test_third_identical_batch_persists_stop_notice(monkeypatch):
    saved = []
    context, phases = _context(native=False)
    signature = tool_batch_flow.batch_signature([_call()])
    monkeypatch.setattr(
        tool_batch_flow,
        "_save_message",
        lambda session, user_id, message: saved.append(message),
    )

    outcome = tool_batch_flow.evaluate_progress(
        context,
        tool_batch_flow.ProgressState(signature, 2),
        [_call()],
    )

    assert outcome.action is tool_batch_flow.ProgressAction.STOP_RUN
    assert context.conversation[-1]["role"] == "user"
    assert saved[0].tags == "system_notice_no_progress_loop"
    assert phases == ["idle"]


def test_batch_executor_merges_exact_duplicates():
    conversation = []
    executed = []
    duplicates = []
    calls = [_call("call-1"), _call("call-2"), _call("call-3", "y")]

    action = tool_batch_flow.execute_turn_batch(
        conversation,
        calls,
        True,
        lambda call, pending: executed.append((call, pending)) or TurnCallAction.NEXT_CALL,
        duplicates.append,
    )

    assert action is TurnCallAction.NEXT_CALL
    assert [item[0]["id"] for item in executed] == ["call-1", "call-3"]
    assert [item["id"] for item in duplicates] == ["call-2"]
    assert conversation[0]["tool_call_id"] == "call-2"
