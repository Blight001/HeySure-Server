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


def _setup(*, task_runtime=False, allowed=None):
    return worker_setup.WorkerSetup(
        user=SimpleNamespace(),
        max_steps=40,
        config=SimpleNamespace(mcp_enabled=True),
        api_key="secret",
        base_url="https://api.anthropic.com",
        model="model-a",
        system_prompt="prompt",
        warning_template="warning",
        auto_control={},
        task_job=None,
        is_task_runtime=task_runtime,
        effective_tool_allowlist=frozenset(
            allowed or {"mcp.describe+tool", "todo.manage", "knowledge.search"}
        ),
        history=[],
        conversation=[],
    )


def test_prepare_capabilities_restores_current_tools_and_applies_preset(monkeypatch):
    request = _request("session-a")
    setup = _setup()
    monkeypatch.setattr(
        worker_setup.mcp_session_context,
        "described_tool_versions",
        lambda *args, **kwargs: {"knowledge.search": "v2", "stale.tool": "v1"},
    )
    monkeypatch.setattr(
        "tools.introspection.current_tool_schema_versions",
        lambda user_id, names: {"knowledge.search": "v2", "stale.tool": "v3"},
    )
    monkeypatch.setattr(
        worker_setup,
        "resolve_session_preset_entry",
        lambda *args: {"provider": "openai", "tool_protocol": "markup"},
    )
    monkeypatch.setattr(
        worker_setup,
        "heysure_provider_session_id",
        lambda *args: "provider-session",
    )

    result = worker_setup.prepare_capabilities(FakeSession(setup.user), request, setup)

    assert result.headers["Authorization"] == "Bearer secret"
    assert result.headers["X-HeySure-Session-ID"] == "provider-session"
    assert result.mcp_active is True
    assert result.exposed_tool_allowlist == frozenset(
        {"mcp.describe+tool", "knowledge.search"}
    )
    assert result.provider == "openai_compat"
    assert result.tool_protocol == "markup"


def test_prepare_capabilities_preexposes_required_task_tools(monkeypatch):
    request = _request()
    setup = _setup(task_runtime=True)
    monkeypatch.setattr(
        worker_setup.mcp_session_context,
        "described_tool_versions",
        lambda *args, **kwargs: {},
    )
    monkeypatch.setattr(worker_setup, "resolve_session_preset_entry", lambda *args: None)
    monkeypatch.setattr(worker_setup, "_detect_provider", lambda base_url: "anthropic")

    result = worker_setup.prepare_capabilities(FakeSession(setup.user), request, setup)

    expected = {"mcp.describe+tool"} | (
        set(worker_setup.TASK_RUNTIME_REQUIRED_TOOLS)
        & set(setup.effective_tool_allowlist)
    )
    assert result.exposed_tool_allowlist == frozenset(expected)
    assert result.provider == "anthropic"
    assert result.tool_protocol == "auto"


def test_prepare_capabilities_logs_and_ignores_restore_failure(monkeypatch, caplog):
    request = _request("session-a")
    setup = _setup()
    monkeypatch.setattr(
        worker_setup.mcp_session_context,
        "described_tool_versions",
        lambda *args, **kwargs: (_ for _ in ()).throw(RuntimeError("cache unavailable")),
    )
    monkeypatch.setattr(worker_setup, "resolve_session_preset_entry", lambda *args: None)

    with caplog.at_level("ERROR"):
        result = worker_setup.prepare_capabilities(FakeSession(setup.user), request, setup)

    assert result.exposed_tool_allowlist == frozenset({"mcp.describe+tool"})
    assert "restore described MCP tools failed" in caplog.text
