from mcp_runtime.mcp.registry import registry as _registry  # noqa: F401 - production import order
from api.services.knowledge import librarian_core, librarian_service


def _write_procedural_topic(tmp_path):
    topics = tmp_path / "topics"
    topics.mkdir(exist_ok=True)
    path = topics / "procedure.md"
    path.write_text(
        "---\n"
        "memory_id: mem_procedure\n"
        "title: 程序性知识\n"
        "triggers: 流程, 回归\n"
        "status: active\n"
        "---\n"
        "原始正文\n",
        encoding="utf-8",
    )
    return path


def test_every_listed_procedural_topic_supports_get_edit_and_delete(monkeypatch, tmp_path):
    monkeypatch.setattr(librarian_core, "_kb_root", lambda _user_id: str(tmp_path))
    path = _write_procedural_topic(tmp_path)

    listed = librarian_service.list_inheritance_thoughts(user_id=1, limit=20)
    assert [item["id"] for item in listed["items"]] == ["mem_procedure"]

    detail = librarian_service.read_inheritance_thought(
        user_id=1,
        thought_id="mem_procedure",
    )
    assert detail["skill_card"] == "原始正文\n"
    assert detail["lines"] == [{"line": 1, "text": "原始正文"}]

    edited = librarian_service.edit_inheritance_thought(
        user_id=1,
        thought_id="mem_procedure",
        arguments={
            "mode": "append",
            "text": "追加正文",
            "expected_sha256": detail["content_sha256"],
        },
    )
    assert edited["updated"] is True
    assert "triggers: 流程, 回归" in path.read_text(encoding="utf-8")
    assert path.read_text(encoding="utf-8").endswith("原始正文\n追加正文\n")

    deleted = librarian_service.delete_inheritance_thought(
        user_id=1,
        thought_id="mem_procedure",
    )
    assert deleted == {"deleted": True, "id": "mem_procedure"}
    assert not path.exists()


def test_manual_thought_preserves_and_updates_search_metadata(monkeypatch, tmp_path):
    monkeypatch.setattr(librarian_core, "_kb_root", lambda _user_id: str(tmp_path))

    created = librarian_service.create_inheritance_thought(
        user_id=1,
        name="手工知识",
        content=(
            "---\n"
            "tags: [创建触发词, frontmatter]\n"
            "---\n"
            "# 手工知识\n\n正文。\n"
        ),
        summary="初始摘要",
        endpoint_kind="any",
    )
    assert created["triggers"] == ["创建触发词", "frontmatter"]

    detail = librarian_service.read_inheritance_thought(
        user_id=1,
        thought_id=created["id"],
    )
    edited = librarian_service.edit_inheritance_thought(
        user_id=1,
        thought_id=created["id"],
        arguments={
            "name": "已整理手工知识",
            "summary": "更新后的摘要",
            "triggers": ["整理", "检索", "整理"],
            "expected_sha256": detail["content_sha256"],
        },
    )
    assert edited["name"] == "已整理手工知识"
    assert edited["summary"] == "更新后的摘要"
    assert edited["triggers"] == ["整理", "检索"]

    listed = librarian_service.list_inheritance_thoughts(
        user_id=1,
        query="检索",
        limit=20,
        compact=False,
    )
    assert listed["total"] == 1
    assert listed["items"][0]["id"] == created["id"]


def test_manual_thought_explicit_triggers_override_frontmatter(monkeypatch, tmp_path):
    monkeypatch.setattr(librarian_core, "_kb_root", lambda _user_id: str(tmp_path))

    created = librarian_service.create_inheritance_thought(
        user_id=1,
        name="显式触发词",
        content="---\ntags: [旧标签]\n---\n正文",
        triggers=["新标签", "新标签", "第二标签"],
        endpoint_kind="any",
    )

    assert created["triggers"] == ["新标签", "第二标签"]
