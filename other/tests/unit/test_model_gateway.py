from types import SimpleNamespace

import pytest

from ai_runtime.inference import model_gateway


def _request(provider="openai", tools=None):
    return model_gateway.ModelTurnRequest(
        run_id="run-a",
        provider=provider,
        base_url="http://model",
        api_key="key",
        model="model-a",
        conversation=[{"role": "user", "content": "hello"}],
        provider_tools=tools or [],
        native_name_map={"workspace_read": "workspace.read"},
        headers={"Authorization": "Bearer key"},
    )


def test_anthropic_request_routes_to_anthropic_stream(monkeypatch):
    observed = {}
    sentinel = object()
    monkeypatch.setattr(
        model_gateway,
        "stream_turn_anthropic",
        lambda **kwargs: observed.update(kwargs) or sentinel,
    )

    result = model_gateway.run_model_turn(_request(provider="anthropic"))

    assert result is sentinel
    assert observed["run_id"] == "run-a"
    assert observed["convo"][0]["content"] == "hello"
    assert observed["native_tool_name_map"] == {
        "workspace_read": "workspace.read"
    }


def test_openai_retries_without_parallel_tools_when_provider_rejects(monkeypatch):
    payloads = []
    first = SimpleNamespace(
        ok=False,
        text="unknown parameter parallel_tool_calls",
        closed=False,
        close=lambda: setattr(first, "closed", True),
    )
    second = SimpleNamespace(ok=True, text="", close=lambda: None)
    responses = iter([first, second])

    def fake_post(url, **kwargs):
        payloads.append(dict(kwargs["json"]))
        return next(responses)

    monkeypatch.setattr(model_gateway, "ai_http_post", fake_post)
    monkeypatch.setattr(
        model_gateway,
        "stream_turn_openai_compat",
        lambda **kwargs: kwargs["response"],
    )

    result = model_gateway.run_model_turn(
        _request(tools=[{"type": "function", "function": {"name": "workspace_read"}}])
    )

    assert result is second
    assert payloads[0]["parallel_tool_calls"] is True
    assert "parallel_tool_calls" not in payloads[1]
    assert first.closed is True


def test_upstream_error_uses_structured_error_fields():
    response = SimpleNamespace(
        ok=False,
        status_code=400,
        reason="Bad Request",
        url="http://model/chat",
        text='{"error":{"message":"bad input","code":"invalid","type":"request"}}',
        json=lambda: {
            "error": {
                "message": "bad input",
                "code": "invalid",
                "type": "request",
            }
        },
    )

    with pytest.raises(RuntimeError) as error:
        model_gateway.raise_for_upstream_error(response)

    text = str(error.value)
    assert "HTTP 400 Bad Request" in text
    assert "bad input | invalid | request" in text


def test_successful_upstream_response_does_not_raise():
    model_gateway.raise_for_upstream_error(SimpleNamespace(ok=True))
