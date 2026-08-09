from types import SimpleNamespace

from ai_runtime.inference import tool_metadata


def _context(exposed=None):
    return tool_metadata.ToolMetadataContext(
        session=SimpleNamespace(),
        user_id=9,
        ai_config_id=3,
        ai_kind="assistant",
        session_id="session-a",
        session_name="旧名称",
        allowed_tools=frozenset({"workspace.read"}),
        exposed_tools=exposed if exposed is not None else set(),
    )


def test_successful_current_session_rename_returns_new_name():
    renamed = tool_metadata.apply_tool_metadata(
        _context(),
        "conversation.manage",
        {
            "result": {
                "success": True,
                "action": "rename",
                "session_id": "session-a",
                "name": "新名称",
            }
        },
        False,
    )

    assert renamed == "新名称"


def test_rename_for_other_session_is_ignored():
    renamed = tool_metadata.apply_tool_metadata(
        _context(),
        "conversation.manage",
        {"result": {"action": "rename", "session_id": "session-b", "name": "x"}},
        False,
    )

    assert renamed == ""


def test_described_tools_expose_only_allowed_names_and_persist(monkeypatch):
    exposed = set()
    context = _context(exposed)
    observed = {}
    monkeypatch.setattr(
        tool_metadata.mcp_session_context,
        "remember_described_tools",
        lambda session, **kwargs: observed.update(kwargs),
    )

    renamed = tool_metadata.apply_tool_metadata(
        context,
        "mcp.describe+tool",
        {
            "result": {
                "tools": [
                    {"name": "workspace.read"},
                    {"name": "workspace.write"},
                ]
            }
        },
        False,
    )

    assert renamed == ""
    assert exposed == {"workspace.read"}
    assert [item["name"] for item in observed["described"]] == [
        "workspace.read",
        "workspace.write",
    ]


def test_failed_tool_has_no_metadata_side_effect(monkeypatch):
    exposed = set()
    monkeypatch.setattr(
        tool_metadata.mcp_session_context,
        "remember_described_tools",
        lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("unexpected")),
    )

    renamed = tool_metadata.apply_tool_metadata(
        _context(exposed),
        "mcp.describe+tool",
        {"result": {"tools": [{"name": "workspace.read"}]}},
        True,
    )

    assert renamed == ""
    assert exposed == set()


def test_apply_session_rename_updates_saved_message_only_for_new_name():
    saved = SimpleNamespace(session_name="旧名称")

    unchanged = tool_metadata.apply_session_rename(saved, "旧名称", "")
    renamed = tool_metadata.apply_session_rename(saved, unchanged, "新名称")

    assert unchanged == "旧名称"
    assert renamed == "新名称"
    assert saved.session_name == "新名称"
