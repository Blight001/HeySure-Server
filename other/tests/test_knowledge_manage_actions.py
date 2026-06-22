"""Regression tests for knowledge.manage actions via the worker async bridge."""

import pytest
from fastapi import HTTPException

from api.runtime.async_bridge import run_async
from mcp_runtime.mcp.loader import reload_registry
from mcp_runtime.mcp.registry import registry
from tools.knowledge import KNOWLEDGE_MANAGE_SCHEMA, _KNOWLEDGE_ACTIONS
from library.handlers import create_inheritance_thought


def _call(action: str, **extra):
    args = {"action": action, **extra}

    async def invoke():
        return await registry.call("knowledge.manage", 1, args, None)

    return run_async(invoke())


@pytest.fixture(autouse=True)
def _reload_registry():
    reload_registry()


def test_knowledge_manage_read_actions():
    for action in ("read_skills", "read_system_prompts", "read_personas", "list_thoughts"):
        payload = _call(action)
        result = payload.get("result")
        assert isinstance(result, dict), action


def test_knowledge_manage_validation_errors():
    with pytest.raises(HTTPException) as exc:
        _call("install_skill_package")
    assert "package is required" in str(exc.value.detail)

    with pytest.raises(HTTPException) as exc:
        _call("edit_thought")
    assert exc.value.status_code in (400, 404, 422)


def test_knowledge_manage_actions_registered():
    assert len(_KNOWLEDGE_ACTIONS) >= 13


def test_knowledge_manage_survives_repeated_calls():
    for _ in range(5):
        payload = _call("list_thoughts")
        assert isinstance(payload.get("result"), dict)
        payload = _call("read_skills")
        assert isinstance(payload.get("result"), dict)


def test_knowledge_manage_schema_documents_create_fields():
    props = KNOWLEDGE_MANAGE_SCHEMA.get("properties") or {}
    assert "name" in props
    assert "content" in props
    assert props["title"]["description"].startswith("create_thought 时等同 name")
    assert props["text"]["description"].startswith("edit_thought 写入文本")


def test_create_thought_requires_name_and_content():
    with pytest.raises(HTTPException) as exc:
        create_inheritance_thought(1, {"name": "demo"}, None)
    assert "content is required" in str(exc.value.detail)

    with pytest.raises(HTTPException) as exc:
        create_inheritance_thought(1, {"content": "body"}, None)
    assert "name is required" in str(exc.value.detail)


def test_create_thought_accepts_title_text_aliases(monkeypatch):
    captured = {}

    def _fake_create(**kwargs):
        captured.update(kwargs)
        return {"id": "manual/demo-abc", "displayName": kwargs["name"]}

    monkeypatch.setattr(
        "library.handlers.librarian_service.create_inheritance_thought",
        _fake_create,
    )
    result = create_inheritance_thought(
        1,
        {"title": "别名标题", "text": "正文内容", "summary": "摘要"},
        None,
    )
    assert result["id"] == "manual/demo-abc"
    assert captured["name"] == "别名标题"
    assert captured["content"] == "正文内容"
    assert captured["summary"] == "摘要"