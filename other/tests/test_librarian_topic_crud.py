from mcp_runtime.mcp.registry import registry as _registry  # noqa: F401 - production import order
from api.services.knowledge import librarian_core, librarian_service


def _write_topic(tmp_path):
    topics = tmp_path / "topics"
    topics.mkdir()
    path = topics / "editing.md"
    path.write_text(
        "---\n"
        "memory_id: mem_editing\n"
        "title: 可编辑知识\n"
        "triggers: 编辑, 删除\n"
        "status: active\n"
        "---\n"
        "原始正文\n",
        encoding="utf-8",
    )
    return path


def test_update_topic_content_preserves_frontmatter(monkeypatch, tmp_path):
    monkeypatch.setattr(librarian_core, "_kb_root", lambda _user_id: str(tmp_path))
    path = _write_topic(tmp_path)

    detail = librarian_service.update_topic_content(
        user_id=1,
        memory_id="mem_editing",
        content="# 新正文\n\n更新后的内容。\n",
    )

    raw = path.read_text(encoding="utf-8")
    assert "memory_id: mem_editing" in raw
    assert raw.endswith("# 新正文\n\n更新后的内容。\n")
    assert detail["body"] == "# 新正文\n\n更新后的内容。\n"


def test_delete_topic_removes_only_selected_file(monkeypatch, tmp_path):
    monkeypatch.setattr(librarian_core, "_kb_root", lambda _user_id: str(tmp_path))
    path = _write_topic(tmp_path)
    untouched = tmp_path / "topics" / "untouched.md"
    untouched.write_text("保留", encoding="utf-8")

    result = librarian_service.delete_topic(user_id=1, memory_id="mem_editing")

    assert result == {"deleted": True, "memory_id": "mem_editing"}
    assert not path.exists()
    assert untouched.exists()


def test_builtin_entry_cannot_be_edited(monkeypatch, tmp_path):
    monkeypatch.setattr(librarian_core, "_kb_root", lambda _user_id: str(tmp_path))

    try:
        librarian_service.update_topic_content(
            user_id=1,
            memory_id="builtin.inheritance_tools",
            content="blocked",
        )
    except ValueError as exc:
        assert str(exc) == "knowledge entry is not editable"
    else:
        raise AssertionError("built-in knowledge must not be editable")
