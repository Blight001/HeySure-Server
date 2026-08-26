from types import SimpleNamespace

import pytest
from fastapi import HTTPException

# Keep the production MCP registry import order used by the other librarian
# tests; it prevents the builtin tool catalog from observing a partial import.
from mcp_runtime.mcp.registry import registry as _registry  # noqa: F401
from api.services.knowledge import librarian_core, librarian_service
from api.services.knowledge import workspace_skills as skills


def _workspace_fixture(monkeypatch, tmp_path):
    root = tmp_path / "member"
    card = root / "skills" / "firmware-maintainer" / "SKILL.md"
    card.parent.mkdir(parents=True)
    card.write_text(
        "---\n"
        "name: 固件维护\n"
        "description: 智能锁固件排查流程\n"
        "triggers: [固件, 智能锁]\n"
        "endpoint_kind: desktop\n"
        "---\n\n"
        "# 排查步骤\n\n先读取设备版本，再执行回归。\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(
        skills,
        "_configured_members",
        lambda _user_id: [SimpleNamespace(id=21, name="硬件 AI")],
    )
    monkeypatch.setattr(
        skills,
        "member_workspace_dir",
        lambda _user_id, _ai_config_id, create=False: str(root),
    )
    return root, card


def test_workspace_skill_is_discovered_without_registration(monkeypatch, tmp_path):
    _root, card = _workspace_fixture(monkeypatch, tmp_path)

    items = skills.list_workspace_skills(1, ai_config_id=21)

    assert len(items) == 1
    item = items[0]
    assert item["slug"].startswith("workspace:21:")
    assert item["displayName"] == "固件维护"
    assert item["triggers"] == ["固件", "智能锁"]
    assert item["scope"] == "ai"
    assert item["scope_target"] == "21"
    assert item["endpoint_kind"] == "desktop"
    assert item["_absolute_path"] == str(card)


def test_workspace_skill_is_visible_to_global_scope_only_when_declared(monkeypatch, tmp_path):
    root, card = _workspace_fixture(monkeypatch, tmp_path)
    card.write_text(
        card.read_text(encoding="utf-8").replace("endpoint_kind: desktop", "scope: global\nendpoint_kind: desktop"),
        encoding="utf-8",
    )

    assert len(skills.list_workspace_skills(1, ai_config_id=99)) == 1
    assert len(skills.list_workspace_skills(1, ai_config_id=21)) == 1
    assert root.exists()


def test_workspace_skill_read_enforces_owner_and_preserves_body(monkeypatch, tmp_path):
    _root, _card = _workspace_fixture(monkeypatch, tmp_path)
    item = skills.list_workspace_skills(1, ai_config_id=21)[0]

    detail = librarian_service.read_inheritance_thought(user_id=1, thought_id=item["slug"])
    assert detail["skill"]["displayName"] == "固件维护"
    assert "先读取设备版本" in detail["skill_card"]
    with pytest.raises(PermissionError):
        skills.read_workspace_skill(1, item["slug"], ai_config_id=99)


def test_core_knowledge_scan_includes_workspace_skill(monkeypatch, tmp_path):
    _root, _card = _workspace_fixture(monkeypatch, tmp_path)
    monkeypatch.setattr(librarian_core, "_kb_root", lambda _user_id: str(tmp_path / "KnowledgeBase"))

    entries = librarian_core._load_user_knowledge_entries(1)

    assert any(entry["memory_id"].startswith("workspace:21:") for entry in entries)


def test_workspace_skill_body_and_endpoint_updates_preserve_frontmatter(monkeypatch, tmp_path):
    _root, card = _workspace_fixture(monkeypatch, tmp_path)
    item = skills.list_workspace_skills(1, ai_config_id=21)[0]

    updated = skills.update_workspace_skill_content(1, item["slug"], "新的排查正文")
    assert updated["skill_card"] == "新的排查正文\n"
    assert "name: 固件维护" in card.read_text(encoding="utf-8")
    endpoint = skills.update_workspace_skill_endpoint(1, item["slug"], "browser")
    assert endpoint["skill"]["endpoint_kind"] == "browser"


def test_workspace_skill_reference_rejects_path_injection():
    from api.services.knowledge.librarian_clawhub import _normalize_clawhub_slug

    assert _normalize_clawhub_slug("workspace:21:firmware-maintainer-a1b2c3d4")
    with pytest.raises(ValueError):
        _normalize_clawhub_slug("workspace:21:../secret")


def test_chat_skill_context_resolves_workspace_reference_and_enforces_scope(monkeypatch, tmp_path):
    _root, _card = _workspace_fixture(monkeypatch, tmp_path)
    from gateway.routers.chat_run_start_routes import _skill_context
    reference = skills.list_workspace_skills(1, ai_config_id=21)[0]["slug"]

    context = _skill_context(1, 21, [reference])
    assert reference in context
    assert "先读取设备版本" in context
    with pytest.raises(HTTPException) as exc_info:
        _skill_context(1, 99, [reference])
    assert getattr(exc_info.value, "status_code", None) == 403
