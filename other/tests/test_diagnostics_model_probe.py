import json

from gateway.routers import diagnostics


class FakeStreamResponse:
    status_code = 200
    text = ""

    def iter_lines(self):
        chunk = {
            "choices": [{"delta": {"content": "好"}, "finish_reason": None}],
        }
        yield f"data: {json.dumps(chunk, ensure_ascii=False)}".encode()
        yield b"data: [DONE]"

    def close(self):
        pass


def test_probe_model_uses_unsaved_openai_config(monkeypatch):
    captured = {}

    def fake_post(url, **kwargs):
        captured["url"] = url
        captured.update(kwargs)
        return FakeStreamResponse()

    monkeypatch.setattr(diagnostics, "ai_http_post", fake_post)
    result = diagnostics._probe_model(
        "Antigravity",
        "gemini-pro-agent",
        "http://127.0.0.1:8110/v1/chat/completions",
        "ag-secret",
        "回复一个字：好",
        "openai",
    )

    assert result["ok"] is True
    assert result["reply"] == "好"
    assert captured["url"].endswith("/v1/chat/completions")
    assert captured["headers"]["Authorization"] == "Bearer ag-secret"
    assert captured["json"]["model"] == "gemini-pro-agent"
    assert captured["stream"] is True


def test_probe_model_rejects_incomplete_config():
    result = diagnostics._probe_model("Empty", "", "", "", "hello")
    assert result["ok"] is False
    assert "配置不完整" in result["detail"]
