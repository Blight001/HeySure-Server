import pytest

from pydantic import ValidationError

from api.models import AssistantAIConfigUpdate
from api.models.external_control import ExternalControllerRun, ExternalControllerTurn
from api.services.external_control.service import _safe_value
from api.services.external_control.state import RunTransitionError, TurnTransitionError, transition_run, transition_turn
from gateway.routers.external_control import _handoff_markdown, _mcp_tool_definitions, _run_payload


def _run(status: str = "queued") -> ExternalControllerRun:
    return ExternalControllerRun(
        run_id="xrun_test",
        user_id=1,
        ai_config_id=2,
        credential_id=3,
        status=status,
    )


def test_run_happy_path_reaches_immutable_success() -> None:
    row = _run()
    transition_run(row, "leased", 10.0)
    transition_run(row, "running", 11.0)
    transition_run(row, "succeeded", 12.0)

    assert row.status == "succeeded"
    assert row.started_at == 11.0
    assert row.finished_at == 12.0
    with pytest.raises(RunTransitionError):
        transition_run(row, "running", 13.0)


@pytest.mark.parametrize("terminal", ["failed", "cancelled", "expired"])
def test_running_can_reach_each_failure_terminal(terminal: str) -> None:
    row = _run("running")
    transition_run(row, terminal, 20.0)
    assert row.status == terminal
    assert row.finished_at == 20.0


@pytest.mark.parametrize(
    ("current", "target"),
    [("queued", "running"), ("leased", "succeeded"), ("failed", "queued")],
)
def test_illegal_run_transitions_are_rejected(current: str, target: str) -> None:
    with pytest.raises(RunTransitionError):
        transition_run(_run(current), target)


def test_remote_mcp_contract_exposes_only_stable_controller_tools() -> None:
    names = {item["name"] for item in _mcp_tool_definitions()}
    assert names == {
        "heysure.get_context",
        "heysure.list_mcp_tools",
        "heysure.call_mcp",
        "heysure.start_run",
        "heysure.finish_run",
        "heysure.list_events",
        "heysure.list_messages",
        "heysure.claim_message",
        "heysure.renew_message",
        "heysure.reply_message",
        "heysure.fail_message",
    }
    call_schema = next(item for item in _mcp_tool_definitions() if item["name"] == "heysure.call_mcp")
    assert call_schema["inputSchema"]["required"] == ["tool"]


def test_run_payload_keeps_identifier_for_fresh_sqlmodel_instance() -> None:
    row = _run("running")

    payload = _run_payload(row)

    assert payload["run_id"] == "xrun_test"
    assert payload["status"] == "running"
    assert payload["credential_id"] == 3


def test_handoff_uses_environment_variable_instead_of_toml_secret() -> None:
    text = _handoff_markdown("Operator", 42, "https://example.test/mcp/external", "hsc_secret")
    assert "bearer_token_env_var" in text
    assert "HEYSURE_CONTROLLER_TOKEN_42" in text
    assert 'url = "https://example.test/mcp/external"' in text


def test_execution_mode_contract_rejects_unknown_modes() -> None:
    with pytest.raises(ValidationError):
        AssistantAIConfigUpdate(execution_mode="untrusted")


def test_journal_redacts_secret_shaped_result_fields() -> None:
    safe = _safe_value({"ok": True, "access_token": "secret", "nested": {"api-key": "secret"}})
    assert safe == {"ok": True, "access_token": "[redacted]", "nested": {"api-key": "[redacted]"}}


def test_external_conversation_turn_can_recover_a_lease_but_not_a_terminal_state() -> None:
    row = ExternalControllerTurn(
        turn_id="xturn_test",
        user_id=1,
        ai_config_id=2,
        user_message_id=3,
        session_id="session-test",
    )
    transition_turn(row, "running", 10.0)
    transition_turn(row, "queued", 20.0)
    transition_turn(row, "running", 30.0)
    transition_turn(row, "succeeded", 40.0)
    assert row.finished_at == 40.0
    with pytest.raises(TurnTransitionError):
        transition_turn(row, "queued", 50.0)
