import asyncio

from tools import web_search


class _Response:
    def raise_for_status(self):
        return None

    def json(self):
        return {"results": [{"title": "direct"}]}


def test_web_search_calls_configured_api_directly(monkeypatch):
    captured = {}

    class _Client:
        def __init__(self, **kwargs):
            captured["client_kwargs"] = kwargs

        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb):
            return False

        async def post(self, url, **kwargs):
            captured["url"] = url
            captured["request_kwargs"] = kwargs
            return _Response()

    monkeypatch.setattr(web_search, "_get_tavily_api_key", lambda user_id: "tvly-test")
    monkeypatch.setattr(web_search.settings, "tavily_api_url", "https://search.example.test/v1/search")
    monkeypatch.setattr(web_search.httpx, "AsyncClient", _Client)

    result = asyncio.run(web_search._web_search(1, {
        "query": "ClawHub skills",
        "search_depth": "advanced",
        "max_results": 10,
        "include_answer": True,
    }))

    assert result == {"results": [{"title": "direct"}]}
    assert captured["url"] == "https://search.example.test/v1/search"
    assert captured["client_kwargs"] == {"timeout": 120.0, "trust_env": False}
    assert captured["request_kwargs"]["headers"] == {"Authorization": "Bearer tvly-test"}
    assert captured["request_kwargs"]["json"] == {
        "query": "ClawHub skills",
        "search_depth": "advanced",
        "max_results": 10,
        "include_answer": True,
    }
