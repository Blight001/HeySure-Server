from types import SimpleNamespace

import pytest

from api.services.knowledge import kb_store
from ai_runtime.inference import worker_setup
from ai_runtime.inference.run_request import WorkerRequest


class QueryResult:
    def __init__(self, values):
        self.values = values

    def all(self):
        return self.values


class FakeSession:
    def __init__(self, user):
        self.user = user

    def get(self, model, user_id):
        return self.user

    def exec(self, statement):
        return QueryResult([SimpleNamespace(id=1, content="old")])


def _request(session_id="session_task_a"):
    return WorkerRequest.create(
        run_id="run-a",
        user_id=7,
        ai_config_id=3,
        ai_kind="assistant",
        session_id=session_id,
        session_name="任务",
        model_user_content="new",
    )


def test_prepare_worker_builds_frozen_runtime_snapshot(monkeypatch):
    user = SimpleNamespace(
        mcp_max_steps=73,
        mcp_format_error_hint="fallback",
        mcp_history_result_max_chars=9000,
    )
    config = SimpleNamespace(mcp_enabled=True)
    monkeypatch.setattr(
        worker_setup,
        "_resolve_ai_runtime",
        lambda *args: (config, "key", "http://model", "model-a", "base prompt"),
    )
    monkeypatch.setattr(kb_store, "effective_system_value", lambda *args: " warning ")
    monkeypatch.setattr(kb_store, "effective_auto_control_json", lambda *args: {"compression": True})
    monkeypatch.setattr(worker_setup, "normalize_system_auto_control", lambda value: value)
    monkeypatch.setattr(worker_setup, "_load_task_payload_by_session", lambda *args: None)
    monkeypatch.setattr(worker_setup, "_load_task_job_by_session", lambda *args: "job")
    monkeypatch.setattr(
        worker_setup,
        "build_runtime_system_prompt_and_tools",
        lambda *args, **kwargs: ("runtime prompt", {"knowledge.search"}),
    )
    history_calls = []
    monkeypatch.setattr(
        worker_setup,
        "build_conversation_history",
        lambda history, **kwargs: history_calls.append((history, kwargs)) or [{"role": "system"}],
    )

    setup = worker_setup.prepare_worker(FakeSession(user), _request())

    assert setup.user is user
    assert setup.max_steps == 73
    assert setup.warning_template == "warning"
    assert setup.auto_control == {"compression": True}
    assert setup.is_task_runtime is True
    assert setup.effective_tool_allowlist == frozenset({"knowledge.search"})
    assert setup.conversation == [{"role": "system"}]
    assert history_calls[0][1]["mcp_result_max_chars"] == 9000
    assert history_calls[0][1]["model_user_content"] == "new"


def test_prepare_worker_rejects_missing_user():
    with pytest.raises(RuntimeError, match="User not found"):
        worker_setup.prepare_worker(FakeSession(None), _request("session-a"))
